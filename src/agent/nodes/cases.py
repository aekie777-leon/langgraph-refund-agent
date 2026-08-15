"""Finalize deterministic support-case handoff for a graph turn."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.runnables import RunnableConfig

from agent.cases.models import (
    CaseServiceResult,
    CaseTrigger,
    HandoffPolicyInput,
)
from agent.cases.policy import determine_handoff_policy
from agent.cases.runtime import get_case_service
from agent.cases.service import CaseService
from agent.schemas import SemanticRiskCategory, SemanticRiskLevel
from agent.state import RefundState, latest_text_user_message

CaseServiceProvider = Callable[[], CaseService]
AsyncCaseNode = Callable[
    [RefundState, RunnableConfig],
    Awaitable[dict[str, Any]],
]

_RISK_CATEGORIES = frozenset(
    {"self_harm", "violence", "legal", "regulatory", "reputation", "other"}
)


def extract_hard_critical_categories(
    state: RefundState,
) -> tuple[SemanticRiskCategory, ...]:
    """Return categories produced specifically by hard-critical rules."""
    categories: list[SemanticRiskCategory] = []
    for match in state.get("risk_rule_matches", []):
        if match.get("rule_type") != "hard_critical":
            continue
        category = match.get("category")
        if category in _RISK_CATEGORIES:
            categories.append(cast(SemanticRiskCategory, category))
    return tuple(dict.fromkeys(categories))


def build_handoff_policy_input(state: RefundState) -> HandoffPolicyInput:
    """Convert graph state into deterministic handoff-policy facts."""
    return HandoffPolicyInput(
        hard_critical_categories=extract_hard_critical_categories(state),
        semantic_risk_level=state.get("semantic_risk_level"),
        semantic_risk_categories=tuple(state.get("semantic_risk_categories", [])),
        refund_requires_manual_review=bool(state.get("requires_manual_review")),
        human_handoff_confirmed=bool(state.get("human_handoff_confirmed")),
        staff_complaint_severity=state.get("staff_complaint_severity"),
        explicit_other_complaint=bool(state.get("explicit_other_complaint")),
    )


def require_thread_id(config: RunnableConfig) -> str:
    """Return the stable LangGraph thread ID required for persistence."""
    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError(
            "A non-empty configurable.thread_id is required when creating "
            "a support case"
        )
    return thread_id


def build_case_trigger(
    state: RefundState,
    config: RunnableConfig,
) -> CaseTrigger:
    """Build one idempotent persistence trigger from the current turn."""
    message = latest_text_user_message(state)
    if message is None:
        raise ValueError("A text HumanMessage is required when creating a support case")
    if not isinstance(message.content, str):
        raise TypeError("Support-case handoff requires text message content")
    if not isinstance(message.id, str) or not message.id.strip():
        raise ValueError("The triggering HumanMessage must have a stable message ID")

    excerpt = " ".join(message.content.split())[:500]
    if not excerpt:
        raise ValueError("The triggering message must not be empty")

    hard_categories = extract_hard_critical_categories(state)
    semantic_categories = tuple(state.get("semantic_risk_categories", []))
    risk_categories = tuple(dict.fromkeys((*hard_categories, *semantic_categories)))

    risk_level: SemanticRiskLevel | None = state.get("semantic_risk_level")
    if state.get("risk_hard_critical"):
        risk_level = "critical"

    return CaseTrigger(
        thread_id=require_thread_id(config),
        source_message_id=message.id,
        order_id=state.get("order_id") or state.get("last_order_id"),
        risk_level=risk_level,
        risk_categories=risk_categories,
        triggering_message_excerpt=excerpt,
    )


def _case_result_update(result: CaseServiceResult) -> dict[str, Any]:
    """Expose only the case summary needed by graph clients and tests."""
    case = result.case
    return {
        "support_case_action": result.action,
        "support_case_id": str(case.case_id) if case else None,
        "support_case_type": case.case_type if case else None,
        "support_case_priority": case.priority if case else None,
        "support_case_status": case.status if case else None,
        "support_case_reason_codes": list(case.reason_codes) if case else [],
    }


def build_finalize_case_handoff_node(
    service_provider: CaseServiceProvider = get_case_service,
) -> AsyncCaseNode:
    """Build a final handoff node with an injectable case service."""

    async def finalize_case_handoff(
        state: RefundState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        policy_input = build_handoff_policy_input(state)
        decision = determine_handoff_policy(policy_input)

        if not decision.should_create_case:
            return {
                "support_case_action": "not_created",
                "support_case_id": None,
                "support_case_type": None,
                "support_case_priority": None,
                "support_case_status": None,
                "support_case_reason_codes": [],
            }

        trigger = build_case_trigger(state, config)
        result = await service_provider().record_handoff(
            trigger=trigger,
            decision=decision,
        )
        return _case_result_update(result)

    return finalize_case_handoff
