"""Build the isolated LangGraph subflow for v0.5 order operations."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent.cases.models import CaseListQuery
from agent.cases.runtime import get_case_service
from agent.nodes.cases import require_thread_id
from agent.nodes.orders import build_order_detection_node
from agent.operations.models import (
    DeliveryIssueRequest,
    OperationDecision,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.policy import (
    CurrencyThresholds,
    evaluate_delivery_issue,
    evaluate_operation,
)
from agent.operations.provider import OrderProvider, StaleOrderVersionError
from agent.operations.runtime import get_operation_service, get_order_provider
from agent.operations.service import OperationService
from agent.prompts import OPERATION_REQUEST_EXTRACTION_SYSTEM_PROMPT
from agent.schemas import OperationRequestExtraction
from agent.state import RefundState, latest_text_user_message

OperationDestination = Literal[
    "submit_confirmed_operation",
    "confirm_manual_operation",
    "cancel_operation",
    "confirm_delivery_investigation",
    "cancel_delivery_investigation",
]
_THRESHOLDS = CurrencyThresholds({"USD": Decimal("100")})


def _snapshot_from_state(state: RefundState) -> OrderSnapshot:
    """Return the validated provider snapshot stored by the subflow."""
    snapshot = state.get("operation_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("operation snapshot is unavailable")
    return OrderSnapshot.model_validate(snapshot)


def _operation_id(state: RefundState) -> UUID:
    """Return the persisted operation ID required by confirmation nodes."""
    value = state.get("operation_id")
    if not isinstance(value, str):
        raise ValueError("operation_id is unavailable")
    return UUID(value)


def _latest_message_metadata(state: RefundState, config: RunnableConfig) -> tuple[str, str, str]:
    """Build stable thread, message, and excerpt metadata for a persisted request."""
    message = latest_text_user_message(state)
    if message is None or not isinstance(message.content, str):
        raise ValueError("A text HumanMessage is required for an order operation")
    if not isinstance(message.id, str) or not message.id.strip():
        raise ValueError("The operation request requires a stable HumanMessage ID")
    excerpt = " ".join(message.content.split())[:500]
    if not excerpt:
        raise ValueError("The operation request must not be empty")
    return require_thread_id(config), message.id, excerpt


def build_operation_request_extractor_node(extractor: Any):
    """Build the structured extractor used after a current order has been loaded."""

    async def extract_operation_request(state: RefundState) -> dict[str, Any]:
        message = latest_text_user_message(state)
        if message is None:
            return {
                "operation_extraction": {},
                "operation_ambiguous": True,
                "messages": [AIMessage(content="Please describe one order request.")],
            }
        result = await extractor.ainvoke(
            [SystemMessage(content=OPERATION_REQUEST_EXTRACTION_SYSTEM_PROMPT), message]
        )
        if not isinstance(result, OperationRequestExtraction):
            raise TypeError("Operation extractor returned an unexpected result")
        return {
            "operation_extraction": result.model_dump(mode="json"),
            "operation_ambiguous": result.ambiguous,
        }

    return extract_operation_request


def build_load_operation_snapshot_node(provider: OrderProvider):
    """Build a node that loads the current operation-policy snapshot."""

    async def load_operation_snapshot(state: RefundState) -> dict[str, Any]:
        order_id = state.get("order_id")
        if not isinstance(order_id, str):
            return {"operation_snapshot": {}, "operation_lookup_success": False}
        snapshot = await provider.get_order(order_id)
        if snapshot is None:
            return {
                "operation_snapshot": {},
                "operation_lookup_success": False,
                "messages": [AIMessage(content="Order not found. Please enter the correct order number.")],
            }
        return {
            "operation_snapshot": snapshot.model_dump(mode="json"),
            "operation_lookup_success": True,
            "last_order_id": order_id,
        }

    return load_operation_snapshot


def build_evaluate_operation_node(
    provider: OrderProvider,
    service: OperationService,
):
    """Build the deterministic policy and pending-operation persistence node."""

    async def evaluate_operation_request(
        state: RefundState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        extraction = OperationRequestExtraction.model_validate(
            state.get("operation_extraction", {})
        )
        if extraction.ambiguous:
            return {
                "operation_outcome": "ambiguous",
                "messages": [AIMessage(content="Please choose one operation at a time: cancellation, return, or exchange.")],
            }
        snapshot = _snapshot_from_state(state)
        thread_id, source_message_id, excerpt = _latest_message_metadata(state, config)
        if extraction.delivery_issue_type is not None:
            delivery_request = DeliveryIssueRequest(
                thread_id=thread_id,
                source_message_id=source_message_id,
                order_id=snapshot.order_id,
                issue_type=extraction.delivery_issue_type,
                investigation_requested=extraction.investigation_requested,
            )
            delivery_decision = evaluate_delivery_issue(
                delivery_request, snapshot, now=datetime.now(UTC)
            )
            update: dict[str, Any] = {
                "operation_outcome": delivery_decision.outcome,
                "operation_policy_reason_codes": list(delivery_decision.reason_codes),
                "operation_display_reason": delivery_decision.display_reason,
            }
            if delivery_decision.outcome == "manual_review":
                update["domain_case_reason_codes"] = list(delivery_decision.reason_codes)
            update["messages"] = [AIMessage(content=delivery_decision.display_reason)]
            return update

        operation_request = OrderOperationRequest(
            thread_id=thread_id,
            source_message_id=source_message_id,
            order_id=snapshot.order_id,
            operation_type=cast(Any, extraction.operation_type),
            reason=cast(Any, extraction.reason),
            replacement_variant_id=extraction.replacement_variant_id,
        )
        availability = None
        if operation_request.operation_type == "exchange":
            assert operation_request.replacement_variant_id is not None
            availability = await provider.get_replacement_availability(
                order_id=operation_request.order_id,
                replacement_variant_id=operation_request.replacement_variant_id,
            )
        decision = evaluate_operation(
            operation_request,
            snapshot,
            now=datetime.now(UTC),
            thresholds=_THRESHOLDS,
            replacement_available=availability,
        )
        return await _persist_or_respond(
            state=state,
            service=service,
            request=operation_request,
            snapshot=snapshot,
            decision=decision,
            excerpt=excerpt,
        )

    return evaluate_operation_request


async def _persist_or_respond(
    *,
    state: RefundState,
    service: OperationService,
    request: OrderOperationRequest,
    snapshot: OrderSnapshot,
    decision: OperationDecision,
    excerpt: str,
) -> dict[str, Any]:
    """Persist confirmation-gated decisions and format terminal policy results."""
    base = {
        "operation_outcome": decision.outcome,
        "operation_policy_reason_codes": list(decision.reason_codes),
        "operation_display_reason": decision.display_reason,
    }
    if decision.outcome not in ("eligible", "manual_review"):
        return {**base, "messages": [AIMessage(content=decision.display_reason)]}
    result = await service.create_pending_operation(
        request=request,
        snapshot=snapshot,
        decision=decision,
        request_excerpt=excerpt,
    )
    operation = result.operation
    update = {
        **base,
        "operation_service_action": result.action,
        "operation_id": str(operation.operation_id),
        "operation_status": operation.status,
    }
    if result.action == "duplicate_ignored" and operation.status != "pending_confirmation":
        return {
            **update,
            "messages": [AIMessage(content=f"This operation is already {operation.status}.")],
        }
    return update


def operation_confirmation_node(state: RefundState) -> Command[OperationDestination]:
    """Ask once before submitting an operation or opening a delivery investigation."""
    is_delivery_investigation = state.get("operation_id") is None
    decision = interrupt(
        {
            "type": "order_operation_confirmation",
            "operation_id": state.get("operation_id"),
            "question": (
                "Would you like us to open a delivery investigation?"
                if is_delivery_investigation
                else "Would you like to submit this order operation?"
            ),
            "display_reason": state.get("operation_display_reason", ""),
        }
    )
    if decision is False:
        return Command(
            goto="cancel_delivery_investigation"
            if is_delivery_investigation
            else "cancel_operation"
        )
    if decision is not True:
        raise ValueError("Order-operation confirmation must be true or false")
    if is_delivery_investigation:
        return Command(goto="confirm_delivery_investigation")
    if state.get("operation_outcome") == "manual_review":
        return Command(goto="confirm_manual_operation")
    return Command(goto="submit_confirmed_operation")


def build_submit_confirmed_operation_node(
    provider: OrderProvider,
    service: OperationService,
):
    """Build the automatic provider-submission node."""

    async def submit_confirmed_operation(state: RefundState) -> dict[str, Any]:
        operation_id = _operation_id(state)
        try:
            result = await service.submit_confirmed_operation(
                operation_id=operation_id,
                request_id=f"graph-submit:{operation_id}",
                actor="customer",
                provider=provider,
            )
        except StaleOrderVersionError:
            result = await service.update_operation_status(
                operation_id=operation_id,
                target_status="rejected",
                request_id=f"graph-stale:{operation_id}",
                actor="system",
            )
            return {
                "operation_status": result.operation.status,
                "operation_service_action": result.action,
                "messages": [AIMessage(content="The order changed before submission. Please review the current order and submit a new request.")],
            }
        return {
            "operation_status": result.operation.status,
            "operation_service_action": result.action,
            "provider_reference": result.operation.provider_reference,
            "messages": [AIMessage(content="Your order operation has been submitted successfully.")],
        }

    return submit_confirmed_operation


def build_confirm_manual_operation_node(service: OperationService):
    """Build the node that records a confirmed manual-review request."""

    async def confirm_manual_operation(state: RefundState) -> dict[str, Any]:
        operation_id = _operation_id(state)
        result = await service.confirm_operation(
            operation_id=operation_id,
            request_id=f"graph-manual:{operation_id}",
            actor="customer",
        )
        return {
            "operation_status": result.operation.status,
            "operation_service_action": result.action,
            "domain_case_reason_codes": list(result.operation.policy_reason_codes),
            "messages": [AIMessage(content="Your request has been sent for manual review.")],
        }

    return confirm_manual_operation


def build_cancel_operation_node(service: OperationService):
    """Build the node that records customer cancellation of a pending operation."""

    async def cancel_operation(state: RefundState) -> dict[str, Any]:
        operation_id = _operation_id(state)
        result = await service.cancel_pending_operation(
            operation_id=operation_id,
            request_id=f"graph-cancel:{operation_id}",
            actor="customer",
        )
        return {
            "operation_status": result.operation.status,
            "operation_service_action": result.action,
            "messages": [AIMessage(content="The order operation has been cancelled.")],
        }

    return cancel_operation


def confirm_delivery_investigation_node(_state: RefundState) -> dict[str, Any]:
    """Confirm a delivery investigation so the parent graph can create its case."""
    return {
        "operation_status": "submitted",
        "messages": [AIMessage(content="A delivery investigation has been opened.")],
    }


def cancel_delivery_investigation_node(_state: RefundState) -> dict[str, Any]:
    """Prevent a declined delivery investigation from creating a support case."""
    return {
        "operation_status": "cancelled",
        "domain_case_reason_codes": [],
        "messages": [AIMessage(content="The delivery investigation has not been opened.")],
    }


def build_attach_operation_case_node(service: OperationService):
    """Build a final node that links a newly persisted case to an operation."""

    async def attach_operation_case(state: RefundState) -> dict[str, Any]:
        operation_id = state.get("operation_id")
        case_id = state.get("support_case_id")
        if (
            not isinstance(operation_id, str)
            or not isinstance(case_id, str)
            or state.get("operation_status") != "manual_review"
        ):
            return {}
        result = await service.attach_support_case(
            operation_id=UUID(operation_id),
            support_case_id=UUID(case_id),
            request_id=f"graph-case:{operation_id}:{case_id}",
            actor="system",
        )
        return {"operation_service_action": result.action}

    return attach_operation_case


def build_support_case_status_node(service_provider=get_case_service):
    """Build a current-thread-only support-case status responder."""

    async def support_case_status(_state: RefundState, config: RunnableConfig) -> dict[str, Any]:
        try:
            thread_id = require_thread_id(config)
        except ValueError:
            return {
                "messages": [
                    AIMessage(
                        content="I need this conversation's thread ID to look up support-case status."
                    )
                ]
            }
        page = await service_provider().list_cases(CaseListQuery(thread_id=thread_id))
        if not page.items:
            return {"messages": [AIMessage(content="There are no support cases for this conversation.")]}
        lines = ["Support cases for this conversation:"]
        for case in page.items:
            lines.append(
                f"{case.case_id}: {case.case_type}, {case.priority}, {case.status}"
            )
        return {"messages": [AIMessage(content="\n".join(lines))]}

    return support_case_status


def build_operation_subgraph(
    *,
    order_detector: Any,
    extractor: Any,
    provider: OrderProvider | None = None,
    service: OperationService | None = None,
):
    """Compile the v0.5 order-operation child graph."""
    resolved_provider = provider or get_order_provider()
    resolved_service = service or get_operation_service()
    workflow = StateGraph[RefundState, None, RefundState, RefundState](RefundState)
    workflow.add_node(
        "detect_operation_order",
        RunnableLambda(build_order_detection_node(order_detector)),
    )
    workflow.add_node(
        "load_operation_snapshot",
        RunnableLambda(build_load_operation_snapshot_node(resolved_provider)),
    )
    workflow.add_node(
        "extract_operation_request",
        RunnableLambda(build_operation_request_extractor_node(extractor)),
    )
    workflow.add_node(
        "evaluate_operation_request",
        RunnableLambda(build_evaluate_operation_node(resolved_provider, resolved_service)),
    )
    workflow.add_node("operation_confirmation", operation_confirmation_node)
    workflow.add_node("submit_confirmed_operation", build_submit_confirmed_operation_node(resolved_provider, resolved_service))
    workflow.add_node("confirm_manual_operation", build_confirm_manual_operation_node(resolved_service))
    workflow.add_node("cancel_operation", build_cancel_operation_node(resolved_service))
    workflow.add_node(
        "confirm_delivery_investigation",
        RunnableLambda(confirm_delivery_investigation_node),
    )
    workflow.add_node(
        "cancel_delivery_investigation",
        RunnableLambda(cancel_delivery_investigation_node),
    )
    workflow.add_edge(START, "detect_operation_order")
    workflow.add_conditional_edges("detect_operation_order", lambda state: "load" if state.get("order_id") else "end", {"load": "load_operation_snapshot", "end": END})
    workflow.add_conditional_edges("load_operation_snapshot", lambda state: "extract" if state.get("operation_lookup_success") else "end", {"extract": "extract_operation_request", "end": END})
    workflow.add_edge("extract_operation_request", "evaluate_operation_request")
    workflow.add_conditional_edges(
        "evaluate_operation_request",
        lambda state: (
            "confirm"
            if (
                (
                    state.get("operation_id") is not None
                    and state.get("operation_status") == "pending_confirmation"
                )
                or (
                    state.get("operation_id") is None
                    and state.get("operation_outcome") == "manual_review"
                    and state.get("domain_case_reason_codes")
                )
            )
            else "end"
        ),
        {"confirm": "operation_confirmation", "end": END},
    )
    workflow.add_edge("submit_confirmed_operation", END)
    workflow.add_edge("confirm_manual_operation", END)
    workflow.add_edge("cancel_operation", END)
    workflow.add_edge("confirm_delivery_investigation", END)
    workflow.add_edge("cancel_delivery_investigation", END)
    return workflow.compile()
