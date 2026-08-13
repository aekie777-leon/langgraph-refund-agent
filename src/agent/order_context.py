"""Deterministic order-number and conversation-reference helpers."""

import re

ORDER_ID_PATTERN = re.compile(r"ORD-\d{5}")
PREVIOUS_ORDER_REFERENCE_PATTERNS = (
    re.compile(r"\b(?:this|that|the same|previous|last)\s+order\b", re.IGNORECASE),
    re.compile(r"\b(?:refund|return)\s+(?:it|this|that)\b", re.IGNORECASE),
    re.compile(r"(?:这个|那个|这笔|那笔|刚才的|上一个|上一笔|同一个|同一笔)订单"),
)


def references_previous_order(text: str) -> bool:
    """Return whether the user explicitly refers to the previous valid order."""
    return any(pattern.search(text) for pattern in PREVIOUS_ORDER_REFERENCE_PATTERNS)
