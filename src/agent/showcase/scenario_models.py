"""Deterministic model adapters that drive the real Graph in showcase mode."""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage

from agent.schemas import (
    FormalComplaintDetection,
    Intent,
    OperationRequestExtraction,
    OrderDetection,
    Route,
    SemanticRiskDetection,
)


def _message_text(messages: Sequence[Any]) -> str:
    content = messages[-1].content if messages else ""
    return content if isinstance(content, str) else str(content)


class ShowcaseOrderDetector:
    """Extract the stable demonstration order format without external calls."""

    async def ainvoke(self, messages: Sequence[Any]) -> OrderDetection:
        """Return the last complete demonstration order number."""
        match = re.search(r"\bORD-\d{5}\b", _message_text(messages), re.IGNORECASE)
        return OrderDetection(
            has_order_id=match is not None,
            order_id=match.group(0).upper() if match else None,
        )


class ShowcaseRouter:
    """Route the documented showcase phrases into existing workflow intents."""

    async def ainvoke(self, messages: Sequence[Any]) -> Route:
        """Select one existing route without changing business policy."""
        text = _message_text(messages).lower()
        handoff = any(
            phrase in text
            for phrase in (
                "human agent",
                "real person",
                "human support",
                "真人客服",
                "人工客服",
            )
        )
        if any(phrase in text for phrase in ("case status", "support request status", "工单状态")):
            step: Intent = "support_case_status"
        elif any(word in text for word in ("cancel", "取消")):
            step = "cancellation_request"
        elif any(word in text for word in ("exchange", "换货")):
            step = "exchange_request"
        elif any(
            word in text
            for word in ("tracking", "not received", "delivery failed", "物流", "未收到")
        ):
            step = "delivery_issue"
        elif any(word in text for word in ("return", "退货")):
            step = "return_request"
        elif handoff or any(
            word in text
            for word in ("complaint", "terrible", "grievance", "employee", "投诉", "客服态度")
        ):
            step = "complaint"
        elif any(word in text for word in ("where", "order status", "查询订单", "订单状态")):
            step = "order_inquiry"
        else:
            step = "refund_request"
        return Route(step=step, human_handoff_requested=handoff)


class ShowcaseRiskClassifier:
    """Classify only the documented synthetic semantic-risk phrases."""

    async def ainvoke(self, messages: Sequence[Any]) -> SemanticRiskDetection:
        """Return a bounded synthetic risk result for the showcase catalog."""
        text = _message_text(messages).lower()
        if any(phrase in text for phrase in ("immediate semantic danger", "马上伤害")):
            return SemanticRiskDetection(
                risk_level="high",
                categories=["violence"],
                reason="The synthetic scenario contains an immediate violence risk.",
            )
        if any(phrase in text for phrase in ("consumer protection", "formal complaint", "监管投诉")):
            return SemanticRiskDetection(
                risk_level="medium",
                categories=["regulatory"],
                reason="The synthetic scenario includes a regulatory escalation.",
            )
        if any(phrase in text for phrase in ("head against the wall", "撞墙")):
            return SemanticRiskDetection(
                risk_level="medium",
                categories=["self_harm"],
                reason="The synthetic scenario includes ambiguous self-harm language.",
            )
        return SemanticRiskDetection(
            risk_level="none",
            categories=[],
            reason="No semantic risk is present in the synthetic scenario.",
        )


class ShowcaseComplaintClassifier:
    """Classify documented complaint examples without a model service."""

    async def ainvoke(self, messages: Sequence[Any]) -> FormalComplaintDetection:
        """Return a structured complaint classification."""
        text = _message_text(messages).lower()
        if any(phrase in text for phrase in ("employee insulted", "客服辱骂")):
            return FormalComplaintDetection(
                complaint_kind="staff_conduct",
                staff_complaint_severity="medium",
                reason="The synthetic scenario explicitly reports staff misconduct.",
            )
        if any(phrase in text for phrase in ("official grievance", "正式投诉")):
            return FormalComplaintDetection(
                complaint_kind="other_formal",
                reason="The synthetic scenario explicitly requests a formal complaint.",
            )
        return FormalComplaintDetection(
            complaint_kind="ordinary",
            reason="The synthetic scenario expresses ordinary dissatisfaction.",
        )


class ShowcaseOperationExtractor:
    """Extract the narrow documented operation fields for demo orders."""

    async def ainvoke(self, messages: Sequence[Any]) -> OperationRequestExtraction:
        """Return one valid existing operation contract."""
        text = _message_text(messages).lower()
        has_cancel = any(word in text for word in ("cancel", "取消"))
        has_exchange = any(word in text for word in ("exchange", "换货"))
        if has_cancel and has_exchange:
            return OperationRequestExtraction(ambiguous=True)
        if has_cancel:
            return OperationRequestExtraction(
                operation_type="cancellation",
                reason="no_longer_needed",
                ambiguous=False,
            )
        if any(word in text for word in ("tracking", "物流")):
            return OperationRequestExtraction(
                delivery_issue_type="tracking_stalled",
                ambiguous=False,
            )
        if has_exchange:
            return OperationRequestExtraction(
                operation_type="exchange",
                reason="changed_mind",
                replacement_variant_id=(
                    "variant-unavailable"
                    if "unavailable" in text or "无货" in text
                    else "variant-blue"
                ),
                ambiguous=False,
            )
        return OperationRequestExtraction(
            operation_type="return",
            reason="changed_mind",
            ambiguous=False,
        )


class ShowcaseComplaintModel:
    """Generate one deliberately non-committal complaint response."""

    async def ainvoke(self, _messages: Sequence[Any]) -> AIMessage:
        """Avoid inventing order, refund, or investigation outcomes."""
        return AIMessage(
            content=(
                "I understand your concern. I can document it for the support team "
                "without promising an outcome that has not been reviewed."
            )
        )
