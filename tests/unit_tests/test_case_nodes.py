"""Unit tests for graph-to-case handoff adaptation."""

from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from agent.cases.repository import CasePersistenceError
from agent.cases.service import CaseService
from agent.nodes.cases import (
    build_case_trigger,
    build_finalize_case_handoff_node,
    build_handoff_policy_input,
    extract_hard_critical_categories,
)
from tests.fakes.identity import config_with_identity
from tests.support_cases import InMemoryCaseRepository

pytestmark = pytest.mark.anyio


def _config(thread_id: str = "thread-1"):
    return config_with_identity("customer", thread_id=thread_id)


def _hard_risk_state():
    return {
        "messages": [HumanMessage(id="message-1", content="I will kill you.")],
        "risk_hard_critical": True,
        "risk_rule_matches": [
            {
                "rule_type": "hard_critical",
                "category": "violence",
            },
            {
                "rule_type": "risk_signal",
                "category": "legal",
            },
        ],
        "semantic_risk_level": None,
        "semantic_risk_categories": [],
    }


def test_extract_hard_categories_ignores_signal_matches() -> None:
    assert extract_hard_critical_categories(_hard_risk_state()) == ("violence",)


def test_build_policy_input_uses_structured_graph_facts() -> None:
    policy_input = build_handoff_policy_input(
        {
            "risk_rule_matches": [],
            "semantic_risk_level": "medium",
            "semantic_risk_categories": ["regulatory"],
            "requires_manual_review": True,
            "human_handoff_confirmed": True,
            "staff_complaint_severity": "high",
            "explicit_other_complaint": True,
        }
    )

    assert policy_input.semantic_risk_level == "medium"
    assert policy_input.semantic_risk_categories == ("regulatory",)
    assert policy_input.refund_requires_manual_review is True
    assert policy_input.human_handoff_confirmed is True
    assert policy_input.staff_complaint_severity == "high"
    assert policy_input.explicit_other_complaint is True


def test_hard_rule_trigger_is_recorded_as_critical() -> None:
    trigger = build_case_trigger(_hard_risk_state(), _config())

    assert trigger.thread_id == "thread-1"
    assert trigger.source_message_id == "message-1"
    assert trigger.risk_level == "critical"
    assert trigger.risk_categories == ("violence",)


def test_trigger_normalizes_excerpt_and_uses_current_order() -> None:
    trigger = build_case_trigger(
        {
            "messages": [
                HumanMessage(
                    id="message-2",
                    content="Please   refund\nORD-10002.",
                )
            ],
            "order_id": "ORD-10002",
            "semantic_risk_level": "medium",
            "semantic_risk_categories": ["regulatory"],
            "risk_rule_matches": [],
        },
        _config("thread-2"),
    )

    assert trigger.order_id == "ORD-10002"
    assert trigger.triggering_message_excerpt == "Please refund ORD-10002."


def test_positive_trigger_requires_a_thread_id() -> None:
    with pytest.raises(ValueError, match="configurable.thread_id"):
        build_case_trigger(_hard_risk_state(), {})


def test_positive_trigger_requires_a_stable_message_id() -> None:
    state = _hard_risk_state()
    state["messages"] = [HumanMessage(content="I will kill you.")]

    with pytest.raises(ValueError, match="stable message ID"):
        build_case_trigger(state, _config())


async def test_no_case_decision_does_not_resolve_the_service() -> None:
    def unavailable_provider() -> CaseService:
        raise AssertionError("The service must not be resolved for a no-case turn")

    node = build_finalize_case_handoff_node(unavailable_provider)
    result = await node(
        {
            "messages": [HumanMessage(content="Where is my order?")],
            "semantic_risk_level": "none",
            "semantic_risk_categories": [],
            "risk_rule_matches": [],
            "requires_manual_review": False,
        },
        {},
    )

    assert result["support_case_action"] == "not_created"
    assert result["support_case_id"] is None


async def test_positive_decision_persists_and_returns_case_summary() -> None:
    repository = InMemoryCaseRepository()
    service = CaseService(repository)
    node = build_finalize_case_handoff_node(lambda: service)

    result = await node(_hard_risk_state(), _config())

    assert result["support_case_action"] == "created"
    assert result["support_case_id"] is not None
    assert result["support_case_type"] == "safety_review"
    assert result["support_case_priority"] == "p0"
    assert result["support_case_status"] == "open"
    assert result["support_case_reason_codes"] == ["hard_critical_violence"]
    assert len(repository.events) == 1


async def test_persistence_failure_is_not_hidden() -> None:
    class FailingService:
        async def record_handoff(self, scope, *, trigger, decision):
            raise CasePersistenceError("database unavailable")

    node = build_finalize_case_handoff_node(lambda: cast(CaseService, FailingService()))

    with pytest.raises(CasePersistenceError, match="database unavailable"):
        await node(_hard_risk_state(), _config())
