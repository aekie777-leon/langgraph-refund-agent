"""Unit tests for webhook HMAC-SHA256 signing."""

from datetime import UTC, datetime, timedelta

import pytest

from agent.integrations.signing import (
    TIMESTAMP_FORMAT,
    build_canonical_payload,
    compute_signature,
    format_timestamp,
    signature_is_valid,
)

SECRET = b"top-secret-demo-key"
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
EVENT_ID = "evt-123"
BODY = b'{"order_id":"ORD-10001","status":"processing"}'


def _valid_signature(
    *,
    timestamp: datetime = NOW,
    event_id: str = EVENT_ID,
    raw_body: bytes = BODY,
) -> str:
    return compute_signature(
        secret=SECRET,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )


def _verify(
    *,
    signature: str,
    timestamp: datetime = NOW,
    event_id: str = EVENT_ID,
    raw_body: bytes = BODY,
    now: datetime = NOW,
    max_age_seconds: float = 300.0,
) -> bool:
    return signature_is_valid(
        secret=SECRET,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
        signature=signature,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def test_canonical_payload_format_is_fixed() -> None:
    payload = build_canonical_payload(
        timestamp=NOW,
        event_id=EVENT_ID,
        raw_body=BODY,
    )

    assert payload == b"2026-08-17T12:00:00Z.evt-123." + BODY
    assert format_timestamp(NOW) == "2026-08-17T12:00:00Z"
    assert format_timestamp(NOW) == NOW.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def test_correct_signature_passes() -> None:
    assert _verify(signature=_valid_signature()) is True


def test_body_change_fails() -> None:
    other_body = b'{"order_id":"ORD-10002","status":"processing"}'

    assert _verify(signature=_valid_signature(), raw_body=other_body) is False


def test_event_id_change_fails() -> None:
    assert _verify(signature=_valid_signature(), event_id="evt-124") is False


def test_timestamp_change_fails() -> None:
    other_timestamp = NOW + timedelta(seconds=1)

    assert _verify(signature=_valid_signature(), timestamp=other_timestamp) is False


def test_expired_timestamp_fails() -> None:
    old = NOW - timedelta(minutes=6)

    assert (
        _verify(
            signature=_valid_signature(timestamp=old),
            timestamp=old,
        )
        is False
    )


def test_future_timestamp_fails() -> None:
    future = NOW + timedelta(minutes=6)

    assert (
        _verify(
            signature=_valid_signature(timestamp=future),
            timestamp=future,
        )
        is False
    )


def test_uppercase_signature_fails() -> None:
    assert _verify(signature=_valid_signature().upper()) is False


def test_short_signature_fails() -> None:
    assert _verify(signature=_valid_signature()[:63]) is False


def test_non_hex_signature_fails() -> None:
    assert _verify(signature="z" * 64) is False


def test_empty_signature_fails() -> None:
    assert _verify(signature="") is False


def test_empty_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="secret must not be empty"):
        compute_signature(secret=b"", timestamp=NOW, event_id=EVENT_ID, raw_body=BODY)
    with pytest.raises(ValueError, match="secret must not be empty"):
        signature_is_valid(
            secret=b"",
            timestamp=NOW,
            event_id=EVENT_ID,
            raw_body=BODY,
            signature=_valid_signature(),
            now=NOW,
        )


def test_blank_event_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="event_id must not be empty"):
        build_canonical_payload(timestamp=NOW, event_id="   ", raw_body=BODY)
    with pytest.raises(ValueError, match="event_id must not be empty"):
        _verify(signature=_valid_signature(), event_id="   ")


def test_non_positive_max_age_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_age_seconds"):
        _verify(signature=_valid_signature(), max_age_seconds=0)
    with pytest.raises(ValueError, match="max_age_seconds"):
        _verify(signature=_valid_signature(), max_age_seconds=-5)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_max_age_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="max_age_seconds"):
        _verify(signature=_valid_signature(), max_age_seconds=value)


def test_encoding_boundary_rejects_str_body() -> None:
    with pytest.raises(TypeError, match="bytes"):
        build_canonical_payload(
            timestamp=NOW,
            event_id=EVENT_ID,
            raw_body="not bytes",
        )


def test_same_raw_body_signature_is_stable() -> None:
    assert _valid_signature() == _valid_signature()


def test_secret_never_appears_in_error_messages() -> None:
    with pytest.raises(TypeError) as error:
        build_canonical_payload(
            timestamp=NOW,
            event_id=EVENT_ID,
            raw_body="text",
        )
    assert SECRET.decode() not in str(error.value)

    with pytest.raises(ValueError, match="timezone-aware"):
        _verify(
            signature=_valid_signature(),
            timestamp=datetime(2026, 8, 17, 12, 0, 0),
        )
