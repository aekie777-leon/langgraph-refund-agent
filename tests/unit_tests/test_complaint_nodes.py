"""Unit tests for structured formal-complaint classification."""

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from agent.nodes.complaints import build_formal_complaint_classifier_node
from agent.schemas import FormalComplaintDetection


class FakeFormalComplaintClassifier:
    """Return a configured result and retain the invocation messages."""

    def __init__(self, result: FormalComplaintDetection) -> None:
        self.result = result
        self.messages: list[Any] = []

    async def ainvoke(self, messages):
        self.messages = messages
        return self.result


@pytest.mark.anyio
async def test_staff_complaint_maps_structured_severity() -> None:
    classifier = FakeFormalComplaintClassifier(
        FormalComplaintDetection(
            complaint_kind="staff_conduct",
            staff_complaint_severity="medium",
            reason="The user reported an employee's abusive language.",
        )
    )
    node = build_formal_complaint_classifier_node(classifier)

    result = await node(
        {"messages": [HumanMessage(content="Your employee insulted me.")]}
    )

    assert result == {
        "staff_complaint_severity": "medium",
        "explicit_other_complaint": False,
        "formal_complaint_reason": (
            "The user reported an employee's abusive language."
        ),
    }
    assert "staff-conduct severity" in classifier.messages[0].content.lower()


@pytest.mark.anyio
async def test_explicit_nonstaff_complaint_sets_only_other_flag() -> None:
    node = build_formal_complaint_classifier_node(
        FakeFormalComplaintClassifier(
            FormalComplaintDetection(
                complaint_kind="other_formal",
                reason="The user explicitly filed a delivery complaint.",
            )
        )
    )

    result = await node(
        {
            "messages": [
                HumanMessage(content="I want to lodge an official delivery grievance.")
            ]
        }
    )

    assert result["staff_complaint_severity"] is None
    assert result["explicit_other_complaint"] is True


@pytest.mark.anyio
async def test_ordinary_dissatisfaction_does_not_set_case_facts() -> None:
    node = build_formal_complaint_classifier_node(
        FakeFormalComplaintClassifier(
            FormalComplaintDetection(
                complaint_kind="ordinary",
                reason="The user expressed ordinary dissatisfaction.",
            )
        )
    )

    result = await node(
        {"messages": [HumanMessage(content="The delivery service was terrible.")]}
    )

    assert result["staff_complaint_severity"] is None
    assert result["explicit_other_complaint"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "complaint_kind": "staff_conduct",
            "reason": "Missing severity.",
        },
        {
            "complaint_kind": "ordinary",
            "staff_complaint_severity": "low",
            "reason": "Unexpected severity.",
        },
    ],
)
def test_formal_complaint_schema_rejects_inconsistent_severity(payload) -> None:
    with pytest.raises(ValueError, match="staff_complaint_severity"):
        FormalComplaintDetection.model_validate(payload)
