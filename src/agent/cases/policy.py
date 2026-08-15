"""Apply deterministic policy for support-case creation and lifecycle rules."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, cast

from agent.cases.models import (
    CasePriority,
    CaseStatus,
    CaseType,
    HandoffDecision,
    HandoffPolicyInput,
    HandoffReason,
    StaffComplaintSeverity,
)
from agent.schemas import SemanticRiskCategory

CaseCreatingRiskLevel = Literal["medium", "high", "critical"]


class InvalidCaseStatusTransition(ValueError):
    """Report a case status transition forbidden by the domain policy."""


@dataclass(frozen=True, slots=True)
class _ReasonPolicy:
    case_type: CaseType
    priority: CasePriority
    description: str


_RISK_CATEGORY_ORDER: tuple[SemanticRiskCategory, ...] = (
    "self_harm",
    "violence",
    "legal",
    "regulatory",
    "reputation",
    "other",
)
_CASE_TYPE_RANK: dict[CaseType, int] = {
    "safety_review": 0,
    "staff_conduct_complaint": 1,
    "business_escalation": 2,
    "refund_review": 3,
    "general_support": 4,
    "other_complaint": 5,
}
_PRIORITY_RANK: dict[CasePriority, int] = {
    "p0": 0,
    "p1": 1,
    "p2": 2,
    "p3": 3,
}


def _case_type_rank(case_type: CaseType) -> int:
    """Return the configured precedence rank for a case type."""
    return _CASE_TYPE_RANK[case_type]


def _priority_rank(priority: CasePriority) -> int:
    """Return the configured precedence rank for a case priority."""
    return _PRIORITY_RANK[priority]


_UNRESOLVED_STATUSES: frozenset[CaseStatus] = frozenset(
    {"open", "in_progress", "on_hold"}
)
_ALLOWED_STATUS_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    "open": frozenset({"in_progress"}),
    "in_progress": frozenset({"on_hold", "resolved"}),
    "on_hold": frozenset({"in_progress", "resolved"}),
    "resolved": frozenset({"open"}),
}

_HARD_REASON_BY_CATEGORY: dict[SemanticRiskCategory, HandoffReason] = {
    "self_harm": "hard_critical_self_harm",
    "violence": "hard_critical_violence",
    "legal": "hard_critical_legal",
    "regulatory": "hard_critical_regulatory",
    "reputation": "hard_critical_reputation",
    "other": "hard_critical_other",
}
_SEMANTIC_REASON_BY_LEVEL: dict[
    CaseCreatingRiskLevel,
    dict[SemanticRiskCategory, HandoffReason],
] = {
    "critical": {
        "self_harm": "semantic_critical_self_harm",
        "violence": "semantic_critical_violence",
        "legal": "semantic_critical_legal",
        "regulatory": "semantic_critical_regulatory",
        "reputation": "semantic_critical_reputation",
        "other": "semantic_critical_other",
    },
    "high": {
        "self_harm": "semantic_high_self_harm",
        "violence": "semantic_high_violence",
        "legal": "semantic_high_legal",
        "regulatory": "semantic_high_regulatory",
        "reputation": "semantic_high_reputation",
        "other": "semantic_high_other",
    },
    "medium": {
        "self_harm": "semantic_medium_self_harm",
        "violence": "semantic_medium_violence",
        "legal": "semantic_medium_legal",
        "regulatory": "semantic_medium_regulatory",
        "reputation": "semantic_medium_reputation",
        "other": "semantic_medium_other",
    },
}
_STAFF_REASON_BY_SEVERITY: dict[StaffComplaintSeverity, HandoffReason] = {
    "critical": "staff_conduct_critical",
    "high": "staff_conduct_high",
    "medium": "staff_conduct_medium",
    "low": "staff_conduct_low",
}

_REASON_POLICIES: dict[HandoffReason, _ReasonPolicy] = {
    "hard_critical_self_harm": _ReasonPolicy(
        "safety_review", "p0", "A hard-critical self-harm rule matched."
    ),
    "hard_critical_violence": _ReasonPolicy(
        "safety_review", "p0", "A hard-critical violence rule matched."
    ),
    "hard_critical_legal": _ReasonPolicy(
        "business_escalation", "p1", "A hard-critical legal rule matched."
    ),
    "hard_critical_regulatory": _ReasonPolicy(
        "business_escalation", "p1", "A hard-critical regulatory rule matched."
    ),
    "hard_critical_reputation": _ReasonPolicy(
        "business_escalation", "p1", "A hard-critical reputation rule matched."
    ),
    "hard_critical_other": _ReasonPolicy(
        "business_escalation", "p1", "An uncategorized hard-critical rule matched."
    ),
    "semantic_critical_self_harm": _ReasonPolicy(
        "safety_review", "p0", "Semantic classification found critical self-harm risk."
    ),
    "semantic_critical_violence": _ReasonPolicy(
        "safety_review", "p0", "Semantic classification found critical violence risk."
    ),
    "semantic_critical_legal": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found critical legal risk.",
    ),
    "semantic_critical_regulatory": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found critical regulatory risk.",
    ),
    "semantic_critical_reputation": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found critical reputation risk.",
    ),
    "semantic_critical_other": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found an uncategorized critical risk.",
    ),
    "semantic_high_self_harm": _ReasonPolicy(
        "safety_review", "p0", "Semantic classification found high self-harm risk."
    ),
    "semantic_high_violence": _ReasonPolicy(
        "safety_review", "p0", "Semantic classification found high violence risk."
    ),
    "semantic_high_legal": _ReasonPolicy(
        "business_escalation", "p1", "Semantic classification found high legal risk."
    ),
    "semantic_high_regulatory": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found high regulatory risk.",
    ),
    "semantic_high_reputation": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found high reputation risk.",
    ),
    "semantic_high_other": _ReasonPolicy(
        "business_escalation",
        "p1",
        "Semantic classification found an uncategorized high risk.",
    ),
    "semantic_medium_self_harm": _ReasonPolicy(
        "safety_review", "p2", "Semantic classification found medium self-harm risk."
    ),
    "semantic_medium_violence": _ReasonPolicy(
        "safety_review", "p2", "Semantic classification found medium violence risk."
    ),
    "semantic_medium_legal": _ReasonPolicy(
        "business_escalation", "p2", "Semantic classification found medium legal risk."
    ),
    "semantic_medium_regulatory": _ReasonPolicy(
        "business_escalation",
        "p2",
        "Semantic classification found medium regulatory risk.",
    ),
    "semantic_medium_reputation": _ReasonPolicy(
        "business_escalation",
        "p2",
        "Semantic classification found medium reputation risk.",
    ),
    "semantic_medium_other": _ReasonPolicy(
        "business_escalation",
        "p2",
        "Semantic classification found an uncategorized medium risk.",
    ),
    "refund_manual_review": _ReasonPolicy(
        "refund_review", "p1", "The refund requires manual review."
    ),
    "confirmed_human_request": _ReasonPolicy(
        "general_support", "p2", "The user confirmed a request for human support."
    ),
    "staff_conduct_critical": _ReasonPolicy(
        "staff_conduct_complaint",
        "p0",
        "The user reported critical staff conduct.",
    ),
    "staff_conduct_high": _ReasonPolicy(
        "staff_conduct_complaint", "p1", "The user reported serious staff conduct."
    ),
    "staff_conduct_medium": _ReasonPolicy(
        "staff_conduct_complaint",
        "p1",
        "The user reported material staff misconduct.",
    ),
    "staff_conduct_low": _ReasonPolicy(
        "staff_conduct_complaint", "p2", "The user reported a staff service issue."
    ),
    "explicit_other_complaint": _ReasonPolicy(
        "other_complaint", "p3", "The user explicitly made another formal complaint."
    ),
}


def determine_handoff_policy(policy_input: HandoffPolicyInput) -> HandoffDecision:
    """Return one deterministic case decision from structured policy facts."""
    reasons: list[HandoffReason] = []

    for category in _RISK_CATEGORY_ORDER:
        if category in policy_input.hard_critical_categories:
            reasons.append(_HARD_REASON_BY_CATEGORY[category])

    semantic_level = policy_input.semantic_risk_level
    if semantic_level in ("medium", "high", "critical"):
        semantic_reasons = _SEMANTIC_REASON_BY_LEVEL[
            cast(CaseCreatingRiskLevel, semantic_level)
        ]
        for category in _RISK_CATEGORY_ORDER:
            if category in policy_input.semantic_risk_categories:
                reasons.append(semantic_reasons[category])

    if policy_input.staff_complaint_severity is not None:
        reasons.append(_STAFF_REASON_BY_SEVERITY[policy_input.staff_complaint_severity])
    if policy_input.refund_requires_manual_review:
        reasons.append("refund_manual_review")
    if policy_input.human_handoff_confirmed:
        reasons.append("confirmed_human_request")
    if policy_input.explicit_other_complaint:
        reasons.append("explicit_other_complaint")

    reason_codes = _deduplicate_reasons(reasons)
    if not reason_codes:
        return HandoffDecision(should_create_case=False)

    policies = tuple(_REASON_POLICIES[reason] for reason in reason_codes)
    case_types: tuple[CaseType, ...] = tuple(policy.case_type for policy in policies)
    priorities: tuple[CasePriority, ...] = tuple(policy.priority for policy in policies)
    case_type = min(case_types, key=_case_type_rank)
    priority = min(priorities, key=_priority_rank)

    return HandoffDecision(
        should_create_case=True,
        case_type=case_type,
        priority=priority,
        reason_codes=reason_codes,
        display_reason=build_display_reason(reason_codes),
    )


def build_display_reason(reason_codes: Iterable[HandoffReason]) -> str:
    """Build readable text that never participates in business decisions."""
    unique_reasons = _deduplicate_reasons(reason_codes)
    return " ".join(_REASON_POLICIES[reason].description for reason in unique_reasons)


def validate_case_status_transition(
    current_status: CaseStatus,
    target_status: CaseStatus,
) -> None:
    """Raise when the requested case status transition is not allowed."""
    if current_status == target_status:
        return
    if target_status not in _ALLOWED_STATUS_TRANSITIONS[current_status]:
        raise InvalidCaseStatusTransition(
            f"Invalid case status transition: {current_status!r} -> {target_status!r}"
        )


def should_append_to_existing_case(
    *,
    existing_thread_id: str,
    existing_case_type: CaseType,
    existing_status: CaseStatus,
    incoming_thread_id: str,
    incoming_case_type: CaseType,
) -> bool:
    """Return whether a new trigger belongs to an unresolved existing case."""
    return (
        existing_thread_id == incoming_thread_id
        and existing_case_type == incoming_case_type
        and existing_status in _UNRESOLVED_STATUSES
    )


def select_higher_priority(
    current_priority: CasePriority,
    incoming_priority: CasePriority,
) -> CasePriority:
    """Keep the more urgent priority so merged cases are never downgraded."""
    return min(
        (current_priority, incoming_priority),
        key=_priority_rank,
    )


def _deduplicate_reasons(
    reason_codes: Iterable[HandoffReason],
) -> tuple[HandoffReason, ...]:
    unique: list[HandoffReason] = []
    seen: set[HandoffReason] = set()
    for reason in reason_codes:
        if reason not in seen:
            unique.append(reason)
            seen.add(reason)
    return tuple(unique)
