"""Matrix tests for deterministic order-operation Inbox callback policy."""

import pytest

from agent.integrations.inbox_policy import (
    decide_inbox_outbox_readiness,
    decide_order_operation_callback,
)


@pytest.mark.parametrize(
    ("local", "provider", "action"),
    [
        ("submitted", "accepted", "duplicate"),
        ("submitted", "processing", "apply"),
        ("submitted", "completed", "apply"),
        ("submitted", "rejected", "apply"),
        ("processing", "accepted", "stale"),
        ("processing", "processing", "duplicate"),
        ("processing", "completed", "apply"),
        ("processing", "rejected", "apply"),
        ("completed", "accepted", "stale"),
        ("completed", "processing", "stale"),
        ("completed", "completed", "duplicate"),
        ("completed", "rejected", "conflict"),
        ("rejected", "accepted", "conflict"),
        ("rejected", "processing", "conflict"),
        ("rejected", "completed", "conflict"),
        ("rejected", "rejected", "duplicate"),
    ],
)
def test_order_callback_matrix(local, provider, action) -> None:
    result = decide_order_operation_callback(local_status=local, provider_status=provider, current_provider_reference=None, incoming_provider_reference=None)
    assert result.action == action


@pytest.mark.parametrize("local", ["pending_confirmation", "queued", "manual_review", "cancelled_by_customer"])
def test_non_callback_states_conflict(local) -> None:
    assert decide_order_operation_callback(local_status=local, provider_status="accepted", current_provider_reference=None, incoming_provider_reference=None).action == "conflict"


def test_reference_conflict_never_applies() -> None:
    assert decide_order_operation_callback(local_status="submitted", provider_status="accepted", current_provider_reference="one", incoming_provider_reference="two").action == "conflict"


@pytest.mark.parametrize(("outbox", "expected"), [("pending", "retry"), ("processing", "retry"), ("retry_scheduled", "retry"), ("published", "ready"), ("dead", "dead")])
def test_outbox_readiness(outbox, expected) -> None:
    assert decide_inbox_outbox_readiness(outbox_status=outbox) == expected
