"""Unit tests for deterministic retry classification and delay computation."""

import pytest

from agent.integrations.retry import (
    HTTPStatusError,
    ProviderConnectionError,
    ProviderRejectionError,
    ProviderTimeoutError,
    classify_failure,
    classify_http_status,
    decide_retry,
    retry_delay_seconds,
)


def test_provider_connection_error_is_retryable() -> None:
    assert classify_failure(ProviderConnectionError("connection refused")) == (
        "network_error"
    )

    decision = decide_retry(kind="network_error", attempts_so_far=1)

    assert decision.retryable is True
    assert decision.delay_seconds is not None
    assert decision.exhausted is False


def test_provider_timeout_is_retryable() -> None:
    assert classify_failure(ProviderTimeoutError("timed out")) == "timeout"
    assert decide_retry(kind="timeout", attempts_so_far=1).retryable is True


def test_non_network_oserror_is_not_misclassified() -> None:
    with pytest.raises(ValueError, match="cannot classify"):
        classify_failure(FileNotFoundError("orders.json"))
    with pytest.raises(ValueError, match="cannot classify"):
        classify_failure(PermissionError("denied"))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_retryable_http_statuses(status: int) -> None:
    assert classify_http_status(status) == "http_retryable"
    assert classify_failure(HTTPStatusError(status)) == "http_retryable"
    assert decide_retry(kind="http_retryable", attempts_so_far=1).retryable is True


def test_retry_after_is_honored() -> None:
    decision = decide_retry(
        kind="http_retryable",
        attempts_so_far=1,
        retry_after_seconds=30,
        base_delay_seconds=1,
    )

    assert decision.retryable is True
    assert decision.delay_seconds == 30
    assert decision.retry_after_seconds == 30


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_client_errors_are_not_retryable(status: int) -> None:
    assert classify_http_status(status) == "http_client_error"

    decision = decide_retry(kind="http_client_error", attempts_so_far=1)

    assert decision.retryable is False
    assert decision.delay_seconds is None


def test_provider_business_rejection_is_not_retryable() -> None:
    assert (
        classify_failure(ProviderRejectionError("business rejection"))
        == "provider_rejection"
    )
    assert decide_retry(kind="provider_rejection", attempts_so_far=1).retryable is False


def test_local_validation_error_is_not_retryable() -> None:
    assert classify_failure(ValueError("bad input")) == "validation_error"
    assert decide_retry(kind="validation_error", attempts_so_far=1).retryable is False


def test_eighth_attempt_is_the_last_one() -> None:
    assert (
        decide_retry(kind="network_error", attempts_so_far=7, max_attempts=8).retryable
        is True
    )

    final = decide_retry(kind="network_error", attempts_so_far=8, max_attempts=8)

    assert final.retryable is False
    assert final.exhausted is True
    assert final.delay_seconds is None


def test_exponential_backoff_without_jitter() -> None:
    assert retry_delay_seconds(attempts_so_far=1, base_delay_seconds=1.0) == 1.0
    assert retry_delay_seconds(attempts_so_far=2, base_delay_seconds=1.0) == 2.0
    assert retry_delay_seconds(attempts_so_far=3, base_delay_seconds=1.0) == 4.0


def test_max_delay_seconds_caps_backoff() -> None:
    delay = retry_delay_seconds(
        attempts_so_far=10,
        base_delay_seconds=1.0,
        max_delay_seconds=5.0,
    )

    assert delay == 5.0


def test_retry_after_wins_over_max_delay() -> None:
    delay = retry_delay_seconds(
        attempts_so_far=10,
        base_delay_seconds=1.0,
        max_delay_seconds=5.0,
        retry_after_seconds=30.0,
    )

    assert delay == 30.0


def test_jitter_is_deterministic_with_injected_source() -> None:
    delay = retry_delay_seconds(
        attempts_so_far=2,
        base_delay_seconds=1.0,
        jitter_seconds=0.5,
        random_source=lambda: 0.5,
    )

    assert delay == 2.25  # backoff 2.0 + jitter 0.25


def test_no_jitter_without_a_random_source() -> None:
    assert (
        retry_delay_seconds(
            attempts_so_far=2,
            base_delay_seconds=1.0,
            jitter_seconds=0.5,
        )
        == 2.0
    )


def test_unknown_exception_is_not_misjudged_as_network() -> None:
    with pytest.raises(ValueError, match="cannot classify"):
        classify_failure(RuntimeError("unexpected"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_delay_seconds": 0},
        {"base_delay_seconds": -1},
        {"backoff_multiplier": 0.5},
        {"jitter_seconds": -0.1},
        {"max_delay_seconds": 0},
        {"retry_after_seconds": -1},
    ],
)
def test_invalid_retry_configuration_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        retry_delay_seconds(attempts_so_far=1, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_delay_seconds": float("nan")},
        {"base_delay_seconds": float("inf")},
        {"backoff_multiplier": float("nan")},
        {"backoff_multiplier": float("inf")},
        {"jitter_seconds": float("nan")},
        {"jitter_seconds": float("inf")},
        {"max_delay_seconds": float("nan")},
        {"max_delay_seconds": float("inf")},
        {"retry_after_seconds": float("nan")},
        {"retry_after_seconds": float("inf")},
    ],
)
def test_non_finite_retry_configuration_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="finite"):
        retry_delay_seconds(attempts_so_far=1, **kwargs)


def test_invalid_random_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="random_source"):
        retry_delay_seconds(
            attempts_so_far=2,
            jitter_seconds=1.0,
            random_source=lambda: 1.5,
        )


def test_unknown_failure_kind_is_not_retryable() -> None:
    decision = decide_retry(kind="http_client_error", attempts_so_far=1)

    assert decision.retryable is False
