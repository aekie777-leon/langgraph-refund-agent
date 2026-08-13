"""Unit tests for deterministic order-conversation context."""

import pytest

from agent.order_context import ORDER_ID_PATTERN, references_previous_order


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please refund this order.", True),
        ("Return it, please.", True),
        ("请退刚才的订单。", True),
        ("I want to refund another order.", False),
        ("Refund ORD-1234.", False),
    ],
)
def test_previous_order_reference_detection(text: str, expected: bool) -> None:
    assert references_previous_order(text) is expected


@pytest.mark.parametrize(
    ("order_id", "expected"),
    [
        ("ORD-10001", True),
        ("ORD-99999", True),
        ("ord-10001", False),
        ("ORD-1234", False),
        ("XORD-10001", False),
    ],
)
def test_order_id_pattern_requires_the_complete_format(
    order_id: str,
    expected: bool,
) -> None:
    assert (ORDER_ID_PATTERN.fullmatch(order_id) is not None) is expected
