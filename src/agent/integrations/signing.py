"""HMAC-SHA256 webhook signing with a fixed canonical payload format.

The canonical signing payload is always:

    {timestamp}.{event_id}.{raw_body_bytes}

where ``timestamp`` is UTC, formatted as ``%Y-%m-%dT%H:%M:%SZ`` (second
precision, trailing ``Z``), ``event_id`` is the raw event identifier string,
and ``raw_body_bytes`` is the exact request body as received. The signature is
the lowercase hex HMAC-SHA256 digest of that payload over the shared secret.
Verification recomputes the digest from the same inputs and compares with
``hmac.compare_digest`` (constant time). The raw body is never parsed and
re-serialized before signing.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime
from math import isfinite

SIGNATURE_ALGORITHM = "sha256"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_VALIDITY_WINDOW_SECONDS = 300.0  # five minutes

_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def format_timestamp(timestamp: datetime) -> str:
    """Format an aware datetime as the canonical UTC signing timestamp."""
    _require_aware(timestamp, "timestamp")
    return timestamp.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def build_canonical_payload(
    *,
    timestamp: datetime,
    event_id: str,
    raw_body: bytes,
) -> bytes:
    """Build ``{timestamp}.{event_id}.{raw_body_bytes}`` exactly."""
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must not be empty")
    prefix = f"{format_timestamp(timestamp)}.{event_id}.".encode()
    return prefix + raw_body


def compute_signature(
    *,
    secret: bytes,
    timestamp: datetime,
    event_id: str,
    raw_body: bytes,
) -> str:
    """Return the lowercase hex HMAC-SHA256 signature of the canonical payload."""
    _require_secret(secret)
    payload = build_canonical_payload(
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def signature_is_valid(
    *,
    secret: bytes,
    timestamp: datetime,
    event_id: str,
    raw_body: bytes,
    signature: str,
    now: datetime,
    max_age_seconds: float = DEFAULT_VALIDITY_WINDOW_SECONDS,
) -> bool:
    """Verify the signature and the timestamp window (constant-time compare).

    A signature that is not exactly 64 lowercase hex characters, does not
    match the recomputed digest, or falls outside the validity window returns
    False. Empty secrets, blank event IDs, non-positive windows, and naive
    datetimes raise ValueError; the secret is never included in any exception
    message.
    """
    _require_aware(timestamp, "timestamp")
    _require_aware(now, "now")
    _require_secret(secret)
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    if not isinstance(max_age_seconds, (int, float)) or not isfinite(
        max_age_seconds
    ) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be a finite positive number")
    if not isinstance(signature, str) or _SIGNATURE_PATTERN.fullmatch(signature) is None:
        return False
    expected = compute_signature(
        secret=secret,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    if not hmac.compare_digest(expected, signature):
        return False
    age = abs((now - timestamp).total_seconds())
    return age <= max_age_seconds


def _require_secret(secret: bytes) -> None:
    """Reject missing or empty signing secrets."""
    if not isinstance(secret, bytes):
        raise TypeError("secret must be bytes")
    if not secret:
        raise ValueError("secret must not be empty")


def _require_aware(value: datetime, name: str) -> None:
    """Reject naive datetimes so window math is always well-defined."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
