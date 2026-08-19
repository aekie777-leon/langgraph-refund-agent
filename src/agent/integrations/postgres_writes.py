"""Cursor-scoped SQL write helpers for provider messaging.

These helpers never acquire connections, never open transactions, and are not
exposed to the graph. A future transaction coordinator composes them with
domain-aggregate updates, domain events, and outbox/inbox transitions inside
one PostgreSQL transaction. Lease-guarded helpers return the affected row
count so the caller can enforce fencing.

Attempt finalization helpers require the full fencing identity
(attempt id + aggregate id + lease id + worker id) so a stale or mismatched
caller can never finish another worker's attempt.
"""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from agent.integrations.models import (
    OutboxDeliveryStatus,
    ProviderCapability,
    ProviderCommandEnvelope,
)
from agent.integrations.persistence_models import (
    DeliveryAttemptOutcome,
    InboxAttemptOutcome,
)

_ERROR_MAX = 500


class _AsyncExecutable(Protocol):
    """Anything with an async ``execute`` (AsyncConnection or AsyncCursor)."""

    async def execute(self, query: str, params: Any | None = None) -> Any: ...


def _require_aware(value: datetime, name: str) -> None:
    """Reject naive timestamps before they reach the database."""
    if value.tzinfo is None:
        raise ValueError(f"{name} must be an aware datetime")


def _require_finite_non_negative(value: float, name: str) -> None:
    """Reject NaN, infinity, and negative numbers."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _validate_error_fields(
    *,
    error_code: str | None,
    error_message: str | None,
) -> None:
    """Reject unsafe error fields before any SQL runs.

    Error fields are capped at 500 characters, must not be blank when present,
    and must never carry raw provider responses, bodies, signatures, or
    secrets. There is no silent truncation: oversize or blank values fail
    before the write.
    """
    for name, value in (("error_code", error_code), ("error_message", error_message)):
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if value.strip() == "":
            raise ValueError(f"{name} must not be blank")
        if len(value) > _ERROR_MAX:
            raise ValueError(f"{name} must not exceed {_ERROR_MAX} characters")


async def insert_outbox_message(
    executable: _AsyncExecutable,
    *,
    command: ProviderCommandEnvelope,
    status: OutboxDeliveryStatus,
    available_at: datetime,
    now: datetime,
    provider_capability: ProviderCapability = "order_operation",
) -> None:
    """Insert one outbox row derived from a validated command envelope.

    Only the strong-typed command payload is stored in JSONB; all searchable
    envelope metadata is stored in dedicated columns.
    """
    _require_aware(available_at, "available_at")
    _require_aware(now, "now")
    await executable.execute(
        """
        INSERT INTO integration.outbox_messages (
            command_id, schema_version, idempotency_key, tenant_id, customer_id,
            source_message_id, provider_connection_id, provider_capability,
            command_type, aggregate_type, aggregate_id, expected_order_version,
            payload, status, delivery_cycle, attempts_in_cycle, available_at,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            command.command_id,
            command.schema_version,
            command.idempotency_key,
            command.tenant_id,
            command.customer_id,
            command.source_message_id,
            command.connection_id,
            provider_capability,
            command.command_type,
            command.aggregate_type,
            command.aggregate_id,
            command.expected_order_version,
            Jsonb(command.payload.model_dump(mode="json")),
            status,
            1,
            0,
            available_at,
            now,
            now,
        ),
    )


async def update_outbox_transition(
    executable: _AsyncExecutable,
    *,
    command_id: UUID,
    expected_status: OutboxDeliveryStatus,
    expected_lease_id: UUID | None,
    expected_lease_owner: str | None,
    target_status: OutboxDeliveryStatus,
    updated_at: datetime,
    available_at: datetime | None = None,
    published_at: datetime | None = None,
    dead_at: datetime | None = None,
    attempts_in_cycle: int | None = None,
    last_failure_kind: str | None = None,
    last_error_code: str | None = None,
    last_error_message: str | None = None,
) -> int:
    """Apply a lease-guarded outbox status transition; return affected rows."""
    _require_aware(updated_at, "updated_at")
    _validate_error_fields(error_code=last_error_code, error_message=last_error_message)
    if available_at is not None:
        _require_aware(available_at, "available_at")
    if published_at is not None:
        _require_aware(published_at, "published_at")
    if dead_at is not None:
        _require_aware(dead_at, "dead_at")
    sets = [
        "status = %s",
        "updated_at = %s",
        "lease_id = NULL",
        "lease_owner = NULL",
        "lease_expires_at = NULL",
    ]
    parameters: list[Any] = [target_status, updated_at]
    if available_at is not None:
        sets.append("available_at = %s")
        parameters.append(available_at)
    if published_at is not None:
        sets.append("published_at = %s")
        parameters.append(published_at)
    if dead_at is not None:
        sets.append("dead_at = %s")
        parameters.append(dead_at)
    if attempts_in_cycle is not None:
        sets.append("attempts_in_cycle = %s")
        parameters.append(attempts_in_cycle)
    if last_failure_kind is not None:
        sets.append("last_failure_kind = %s")
        parameters.append(last_failure_kind)
    if last_error_code is not None:
        sets.append("last_error_code = %s")
        parameters.append(last_error_code)
    if last_error_message is not None:
        sets.append("last_error_message = %s")
        parameters.append(last_error_message)

    where = "command_id = %s AND status = %s"
    parameters.extend([command_id, expected_status])
    if expected_lease_id is not None:
        where += " AND lease_id = %s"
        parameters.append(expected_lease_id)
    if expected_lease_owner is not None:
        where += " AND lease_owner = %s"
        parameters.append(expected_lease_owner)

    result = await executable.execute(
        f"UPDATE integration.outbox_messages SET {', '.join(sets)} WHERE {where}",
        parameters,
    )
    return int(result.rowcount)


async def finish_delivery_attempt(
    executable: _AsyncExecutable,
    *,
    attempt_id: UUID,
    command_id: UUID,
    lease_id: UUID,
    worker_id: str,
    finished_at: datetime,
    outcome: DeliveryAttemptOutcome,
    failure_kind: str | None = None,
    http_status: int | None = None,
    provider_operation_id: str | None = None,
    provider_reference: str | None = None,
    safe_error_code: str | None = None,
    safe_error_message: str | None = None,
    retry_after_seconds: float | None = None,
    next_available_at: datetime | None = None,
) -> int:
    """Finalize one open delivery attempt; return affected rows.

    The update matches the full fencing identity (attempt, command, lease,
    worker) and only open attempts.
    """
    _require_aware(finished_at, "finished_at")
    _validate_error_fields(error_code=safe_error_code, error_message=safe_error_message)
    if retry_after_seconds is not None:
        _require_finite_non_negative(retry_after_seconds, "retry_after_seconds")
    if next_available_at is not None:
        _require_aware(next_available_at, "next_available_at")
    result = await executable.execute(
        """
        UPDATE integration.outbox_delivery_attempts
        SET finished_at = %s, outcome = %s, failure_kind = %s, http_status = %s,
            provider_operation_id = %s, provider_reference = %s,
            safe_error_code = %s, safe_error_message = %s,
            retry_after_seconds = %s, next_available_at = %s
        WHERE attempt_id = %s AND command_id = %s AND lease_id = %s
          AND worker_id = %s AND finished_at IS NULL
        """,
        (
            finished_at,
            outcome,
            failure_kind,
            http_status,
            provider_operation_id,
            provider_reference,
            safe_error_code,
            safe_error_message,
            retry_after_seconds,
            next_available_at,
            attempt_id,
            command_id,
            lease_id,
            worker_id,
        ),
    )
    return int(result.rowcount)


async def mark_inbox_processed(
    executable: _AsyncExecutable,
    *,
    inbox_id: UUID,
    lease_id: UUID,
    lease_owner: str,
    processed_at: datetime,
) -> int:
    """Mark a leased inbox message processed; return affected rows."""
    _require_aware(processed_at, "processed_at")
    result = await executable.execute(
        """
        UPDATE integration.inbox_messages
        SET status = 'processed', processed_at = %s, updated_at = %s,
            lease_id = NULL, lease_owner = NULL, lease_expires_at = NULL
        WHERE inbox_id = %s AND lease_id = %s AND lease_owner = %s
          AND status = 'processing'
        """,
        (processed_at, processed_at, inbox_id, lease_id, lease_owner),
    )
    return int(result.rowcount)


async def finish_inbox_attempt(
    executable: _AsyncExecutable,
    *,
    attempt_id: UUID,
    inbox_id: UUID,
    lease_id: UUID,
    worker_id: str,
    finished_at: datetime,
    outcome: InboxAttemptOutcome,
    safe_error_code: str | None = None,
    safe_error_message: str | None = None,
) -> int:
    """Finalize one open inbox processing attempt; return affected rows.

    The update matches the full fencing identity (attempt, inbox, lease,
    worker) and only open attempts.
    """
    _require_aware(finished_at, "finished_at")
    _validate_error_fields(error_code=safe_error_code, error_message=safe_error_message)
    result = await executable.execute(
        """
        UPDATE integration.inbox_processing_attempts
        SET finished_at = %s, outcome = %s,
            safe_error_code = %s, safe_error_message = %s
        WHERE attempt_id = %s AND inbox_id = %s AND lease_id = %s
          AND worker_id = %s AND finished_at IS NULL
        """,
        (
            finished_at,
            outcome,
            safe_error_code,
            safe_error_message,
            attempt_id,
            inbox_id,
            lease_id,
            worker_id,
        ),
    )
    return int(result.rowcount)


async def mark_inbox_failed(
    executable: _AsyncExecutable,
    *,
    inbox_id: UUID,
    lease_id: UUID,
    lease_owner: str,
    failed_at: datetime,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    """Mark a leased inbox message failed; return affected rows."""
    _require_aware(failed_at, "failed_at")
    _validate_error_fields(error_code=error_code, error_message=error_message)
    result = await executable.execute(
        """
        UPDATE integration.inbox_messages
        SET status = 'failed', failed_at = %s, updated_at = %s,
            lease_id = NULL, lease_owner = NULL, lease_expires_at = NULL,
            last_error_code = %s, last_error_message = %s
        WHERE inbox_id = %s AND lease_id = %s AND lease_owner = %s
          AND status = 'processing'
        """,
        (
            failed_at,
            failed_at,
            error_code,
            error_message,
            inbox_id,
            lease_id,
            lease_owner,
        ),
    )
    return int(result.rowcount)


async def schedule_inbox_retry(
    executable: _AsyncExecutable,
    *,
    inbox_id: UUID,
    lease_id: UUID,
    lease_owner: str,
    available_at: datetime,
    updated_at: datetime,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    """Return a leased inbox message to received with a new due time."""
    _require_aware(available_at, "available_at")
    _require_aware(updated_at, "updated_at")
    _validate_error_fields(error_code=error_code, error_message=error_message)
    result = await executable.execute(
        """
        UPDATE integration.inbox_messages
        SET status = 'received', available_at = %s, updated_at = %s,
            lease_id = NULL, lease_owner = NULL, lease_expires_at = NULL,
            last_error_code = %s, last_error_message = %s
        WHERE inbox_id = %s AND lease_id = %s AND lease_owner = %s
          AND status = 'processing'
        """,
        (
            available_at,
            updated_at,
            error_code,
            error_message,
            inbox_id,
            lease_id,
            lease_owner,
        ),
    )
    return int(result.rowcount)
