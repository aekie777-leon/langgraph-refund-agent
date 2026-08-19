"""Deterministic retry classification and delay computation.

This module is pure: it never sleeps, never performs I/O, and never depends on
an HTTP client. Failure classification uses explicit provider exception types
only; random jitter is applied only when a random source is injected, so tests
are fully deterministic.
"""

from collections.abc import Callable
from math import isfinite

from agent.integrations.models import (
    RETRYABLE_FAILURE_KINDS,
    ProviderFailureKind,
    RetryDecision,
)

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0


class HTTPStatusError(RuntimeError):
    """Report an HTTP error status returned by a provider."""

    def __init__(
        self,
        status_code: int,
        message: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Store the status code and build a safe message without secrets."""
        if not isinstance(status_code, int):
            raise TypeError("status_code must be an int")
        if retry_after_seconds is not None and (
            not isfinite(retry_after_seconds) or retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds must be a finite non-negative value")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or f"HTTP {status_code}")


class ProviderConnectionError(RuntimeError):
    """Report a network-level failure while talking to a provider (retryable).

    The HTTP adapter is responsible for translating raw transport exceptions
    into this type; arbitrary ``OSError`` subclasses are never classified as
    network failures.
    """


class ProviderTimeoutError(RuntimeError):
    """Report a provider request timeout (retryable)."""


class ProviderRejectionError(RuntimeError):
    """Report an explicit business rejection by the provider (not retryable)."""


def classify_http_status(status_code: int) -> ProviderFailureKind:
    """Classify an HTTP status code into a retry-relevant failure kind."""
    if status_code in (408, 429) or 500 <= status_code <= 599:
        return "http_retryable"
    if 400 <= status_code <= 499:
        return "http_client_error"
    raise ValueError(f"unexpected HTTP status code: {status_code}")


def classify_failure(error: Exception) -> ProviderFailureKind:
    """Classify an exception into a retry-relevant failure kind.

    Only the explicit provider exception types are recognized. Unknown types
    (including arbitrary ``OSError`` subclasses such as ``FileNotFoundError``)
    raise ValueError so adapters are forced to translate raw provider errors
    explicitly; an unclassified failure must never be misjudged as a retryable
    network error.
    """
    if isinstance(error, ProviderRejectionError):
        return "provider_rejection"
    if isinstance(error, ProviderTimeoutError):
        return "timeout"
    if isinstance(error, ProviderConnectionError):
        return "network_error"
    if isinstance(error, HTTPStatusError):
        return classify_http_status(error.status_code)
    if isinstance(error, ValueError):
        return "validation_error"
    raise ValueError(f"cannot classify failure type {type(error).__name__}")


def retry_delay_seconds(
    *,
    attempts_so_far: int,
    base_delay_seconds: float = BASE_BACKOFF_SECONDS,
    backoff_multiplier: float = BACKOFF_MULTIPLIER,
    retry_after_seconds: float | None = None,
    jitter_seconds: float = 0.0,
    max_delay_seconds: float | None = None,
    random_source: Callable[[], float] | None = None,
) -> float:
    """Return the delay before the next attempt (exponential backoff).

    ``attempts_so_far`` is the 1-based count of attempts already completed.
    The base delay is ``base_delay * multiplier ** (attempts_so_far - 1)``.
    Jitter is ``jitter_seconds * random_source()`` and is applied only when a
    random source is injected and returns a value in ``[0, 1)``.
    ``max_delay_seconds`` caps the backoff (including jitter); a provided
    ``retry_after_seconds`` still wins as a minimum delay.
    """
    _validate_retry_parameters(
        attempts_so_far=attempts_so_far,
        base_delay_seconds=base_delay_seconds,
        backoff_multiplier=backoff_multiplier,
        jitter_seconds=jitter_seconds,
        max_delay_seconds=max_delay_seconds,
        retry_after_seconds=retry_after_seconds,
    )
    exponent = max(attempts_so_far - 1, 0)
    delay = base_delay_seconds * (backoff_multiplier**exponent)
    if jitter_seconds > 0 and random_source is not None:
        sample = random_source()
        if not 0.0 <= sample < 1.0:
            raise ValueError("random_source must return values in [0, 1)")
        delay += jitter_seconds * sample
    if max_delay_seconds is not None:
        delay = min(delay, max_delay_seconds)
    if retry_after_seconds is not None:
        delay = max(delay, retry_after_seconds)
    return delay


def decide_retry(
    *,
    kind: ProviderFailureKind,
    attempts_so_far: int,
    max_attempts: int = MAX_ATTEMPTS,
    retry_after_seconds: float | None = None,
    base_delay_seconds: float = BASE_BACKOFF_SECONDS,
    backoff_multiplier: float = BACKOFF_MULTIPLIER,
    jitter_seconds: float = 0.0,
    max_delay_seconds: float | None = None,
    random_source: Callable[[], float] | None = None,
) -> RetryDecision:
    """Decide whether the next attempt should run and after which delay.

    A kind is retryable only when it belongs to ``RETRYABLE_FAILURE_KINDS``,
    and only while ``attempts_so_far < max_attempts``. When attempts are
    exhausted the decision reports ``exhausted=True`` and ``retryable=False``,
    which is the trigger for the outbox ``dead`` state.
    """
    if attempts_so_far < 0:
        raise ValueError("attempts_so_far must not be negative")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    _validate_retry_parameters(
        attempts_so_far=attempts_so_far,
        base_delay_seconds=base_delay_seconds,
        backoff_multiplier=backoff_multiplier,
        jitter_seconds=jitter_seconds,
        max_delay_seconds=max_delay_seconds,
        retry_after_seconds=retry_after_seconds,
    )
    exhausted = attempts_so_far >= max_attempts
    retryable = kind in RETRYABLE_FAILURE_KINDS and not exhausted
    delay = None
    if retryable:
        delay = retry_delay_seconds(
            attempts_so_far=attempts_so_far,
            base_delay_seconds=base_delay_seconds,
            backoff_multiplier=backoff_multiplier,
            retry_after_seconds=retry_after_seconds,
            jitter_seconds=jitter_seconds,
            max_delay_seconds=max_delay_seconds,
            random_source=random_source,
        )
    return RetryDecision(
        retryable=retryable,
        kind=kind,
        attempts_so_far=attempts_so_far,
        max_attempts=max_attempts,
        delay_seconds=delay,
        retry_after_seconds=retry_after_seconds,
        exhausted=exhausted,
    )


def _validate_retry_parameters(
    *,
    attempts_so_far: int,
    base_delay_seconds: float,
    backoff_multiplier: float,
    jitter_seconds: float,
    max_delay_seconds: float | None,
    retry_after_seconds: float | None,
) -> None:
    """Reject invalid retry configuration before any computation."""
    if attempts_so_far < 0:
        raise ValueError("attempts_so_far must not be negative")
    if not isfinite(base_delay_seconds):
        raise ValueError("base_delay_seconds must be finite")
    if base_delay_seconds <= 0:
        raise ValueError("base_delay_seconds must be positive")
    if not isfinite(backoff_multiplier):
        raise ValueError("backoff_multiplier must be finite")
    if backoff_multiplier < 1.0:
        raise ValueError("backoff_multiplier must be at least 1.0")
    if not isfinite(jitter_seconds):
        raise ValueError("jitter_seconds must be finite")
    if jitter_seconds < 0:
        raise ValueError("jitter_seconds must not be negative")
    if max_delay_seconds is not None:
        if not isfinite(max_delay_seconds):
            raise ValueError("max_delay_seconds must be finite")
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds must be positive when set")
    if retry_after_seconds is not None:
        if not isfinite(retry_after_seconds):
            raise ValueError("retry_after_seconds must be finite")
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
