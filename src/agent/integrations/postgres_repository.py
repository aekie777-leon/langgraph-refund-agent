"""PostgreSQL implementation of the provider messaging repository."""

from datetime import datetime
from math import isfinite
from typing import Any, Mapping
from uuid import UUID, uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.integrations.models import (
    SCHEMA_VERSION,
    DeliveryInvestigationCommandPayload,
    OrderOperationCommandPayload,
    ProviderCommandPayload,
    ProviderFailureKind,
    ProviderWebhookEventData,
)
from agent.integrations.persistence_models import (
    ClaimedInboxMessage,
    ClaimedOutboxMessage,
    InboxMessage,
    InboxProcessingAttempt,
    OutboxDeliveryAttempt,
    OutboxMessage,
    OutboxRedrive,
)
from agent.integrations.postgres_writes import (
    finish_delivery_attempt,
    finish_inbox_attempt,
    schedule_inbox_retry,
    update_outbox_transition,
)
from agent.integrations.postgres_writes import (
    mark_inbox_failed as mark_inbox_failed_cursor,
)
from agent.integrations.repository import (
    DuplicateRedriveRequestError,
    InboxAttemptsExhaustedError,
    InboxEventConflictError,
    IntegrationPersistenceError,
    InvalidRedriveStateError,
    LeaseConflictError,
    OutboxAttemptsExhaustedError,
    OutboxMessageNotFoundError,
)

_OUTBOX_COLUMNS = """
    command_id, schema_version, idempotency_key, tenant_id, customer_id,
    source_message_id, provider_connection_id, provider_capability,
    command_type, aggregate_type, aggregate_id, expected_order_version,
    payload, status, delivery_cycle, attempts_in_cycle, available_at,
    lease_id, lease_owner, lease_expires_at, last_failure_kind,
    last_error_code, last_error_message, created_at, updated_at,
    published_at, dead_at
"""
_INBOX_COLUMNS = """
    inbox_id, provider_connection_id, event_id, tenant_id, schema_version,
    event_type, command_id, aggregate_type, aggregate_id, payload,
    raw_body_sha256, status, processing_attempts, available_at, lease_id,
    lease_owner, lease_expires_at, last_error_code, last_error_message,
    received_at, updated_at, processed_at, failed_at
"""


def _validate_claim_parameters(
    *,
    worker_id: str,
    batch_size: int,
    lease_seconds: float,
) -> None:
    """Reject invalid claim parameters before any SQL runs."""
    if not isinstance(worker_id, str) or worker_id.strip() == "":
        raise ValueError("worker_id must not be blank")
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if not isinstance(lease_seconds, (int, float)) or not isfinite(lease_seconds):
        raise ValueError("lease_seconds must be finite")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")


def _parse_payload(command_type: str, raw: Any) -> ProviderCommandPayload:
    """Parse the JSONB payload into the strong-typed command payload."""
    if command_type == "delivery_investigation":
        return DeliveryInvestigationCommandPayload.model_validate(raw)
    return OrderOperationCommandPayload.model_validate(raw)


def _outbox_from_row(row: Mapping[str, Any]) -> OutboxMessage:
    """Validate one outbox row as an outbox persistence model."""
    payload = _parse_payload(row["command_type"], row["payload"])
    return OutboxMessage.model_validate({**row, "payload": payload})


def _inbox_from_row(row: Mapping[str, Any]) -> InboxMessage:
    """Validate one inbox row as an inbox persistence model."""
    payload = ProviderWebhookEventData.model_validate(row["payload"])
    return InboxMessage.model_validate({**row, "payload": payload})


def _verify_inbox_exact_replay(
    *,
    existing: InboxMessage,
    provider_connection_id: str,
    event_id: str,
    tenant_id: str,
    event: ProviderWebhookEventData,
    raw_body_sha256: str,
) -> None:
    """Reject event_id reuse whose trusted content differs from the original.

    Only deterministic field names are reported; the message never contains
    raw bodies, signatures, secrets, or the full payload.
    """
    conflicts: list[str] = []
    if existing.provider_connection_id != provider_connection_id:
        conflicts.append("provider_connection_id")
    if existing.event_id != event_id:
        conflicts.append("event_id")
    if existing.tenant_id != tenant_id:
        conflicts.append("tenant_id")
    if existing.schema_version != SCHEMA_VERSION:
        conflicts.append("schema_version")
    if existing.event_type != "provider_command_status_changed":
        conflicts.append("event_type")
    if existing.command_id != event.command_id:
        conflicts.append("command_id")
    if existing.aggregate_type != event.aggregate_type:
        conflicts.append("aggregate_type")
    if existing.aggregate_id != event.aggregate_id:
        conflicts.append("aggregate_id")
    if existing.raw_body_sha256 != raw_body_sha256:
        conflicts.append("raw_body_sha256")
    if existing.payload != event:
        conflicts.append("payload")
    if conflicts:
        raise InboxEventConflictError(
            f"event_id '{event_id}' for provider connection "
            f"'{provider_connection_id}' conflicts on: {', '.join(conflicts)}"
        )


def _attempt_from_row(row: Mapping[str, Any]) -> OutboxDeliveryAttempt:
    """Validate one delivery-attempt row as a persistence model."""
    return OutboxDeliveryAttempt.model_validate(row)


def _inbox_attempt_from_row(row: Mapping[str, Any]) -> InboxProcessingAttempt:
    """Validate one inbox-attempt row as a persistence model."""
    return InboxProcessingAttempt.model_validate(row)


class PostgresIntegrationRepository:
    """Implement provider-messaging persistence with an async connection pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store a pool whose lifecycle is owned by the application."""
        self._pool = pool

    # ------------------------------------------------------------- outbox

    async def get_outbox_message(self, command_id: UUID) -> OutboxMessage | None:
        """Return one outbox message by command id."""
        query = f"""
            SELECT {_OUTBOX_COLUMNS}
            FROM integration.outbox_messages
            WHERE command_id = %s
        """
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, (command_id,))
                    row = await cursor.fetchone()
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to read outbox message") from error
        return None if row is None else _outbox_from_row(row)

    async def claim_due_outbox(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: float,
    ) -> list[ClaimedOutboxMessage]:
        """Claim due outbox messages with SKIP LOCKED and create attempts."""
        _validate_claim_parameters(
            worker_id=worker_id,
            batch_size=batch_size,
            lease_seconds=lease_seconds,
        )
        claimed: list[ClaimedOutboxMessage] = []
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            f"""
                            SELECT {_OUTBOX_COLUMNS}
                            FROM integration.outbox_messages
                            WHERE command_id IN (
                                SELECT command_id FROM integration.outbox_messages
                                WHERE status IN ('pending', 'retry_scheduled')
                                  AND available_at <= clock_timestamp()
                                  AND attempts_in_cycle < 8
                                ORDER BY available_at, created_at, command_id
                                FOR UPDATE SKIP LOCKED
                                LIMIT %s
                            )
                            ORDER BY available_at, created_at, command_id
                            """,
                            (batch_size,),
                        )
                        rows = await cursor.fetchall()
                        for row in rows:
                            lease_id = uuid4()
                            attempt_id = uuid4()
                            await cursor.execute(
                                f"""
                                UPDATE integration.outbox_messages
                                SET status = 'processing',
                                    lease_id = %s,
                                    lease_owner = %s,
                                    lease_expires_at = clock_timestamp()
                                        + (%s * interval '1 second'),
                                    attempts_in_cycle = attempts_in_cycle + 1,
                                    updated_at = clock_timestamp()
                                WHERE command_id = %s
                                RETURNING {_OUTBOX_COLUMNS}
                                """,
                                (lease_id, worker_id, lease_seconds, row["command_id"]),
                            )
                            updated_row = await cursor.fetchone()
                            if updated_row is None:
                                raise IntegrationPersistenceError(
                                    "Claim update returned no outbox row"
                                )
                            await cursor.execute(
                                """
                                INSERT INTO integration.outbox_delivery_attempts (
                                    attempt_id, command_id, delivery_cycle,
                                    attempt_number, lease_id, worker_id, started_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    attempt_id,
                                    updated_row["command_id"],
                                    updated_row["delivery_cycle"],
                                    updated_row["attempts_in_cycle"],
                                    lease_id,
                                    worker_id,
                                    updated_row["updated_at"],
                                ),
                            )
                            attempt = OutboxDeliveryAttempt(
                                attempt_id=attempt_id,
                                command_id=updated_row["command_id"],
                                delivery_cycle=updated_row["delivery_cycle"],
                                attempt_number=updated_row["attempts_in_cycle"],
                                lease_id=lease_id,
                                worker_id=worker_id,
                                started_at=updated_row["updated_at"],
                            )
                            payload = _parse_payload(
                                updated_row["command_type"], updated_row["payload"]
                            )
                            claimed.append(
                                ClaimedOutboxMessage.model_validate(
                                    {
                                        **updated_row,
                                        "payload": payload,
                                        "attempt": attempt,
                                    }
                                )
                            )
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to claim outbox messages") from error
        return claimed

    async def renew_outbox_lease(
        self,
        *,
        command_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        """Extend the lease while the worker is still alive.

        Returns ``False`` when the lease already expired: a renew must never
        resurrect an expired lease.
        """
        _validate_claim_parameters(
            worker_id=lease_owner,
            batch_size=1,
            lease_seconds=lease_seconds,
        )
        try:
            async with self._pool.connection() as connection:
                result = await connection.execute(
                    """
                    UPDATE integration.outbox_messages
                    SET lease_expires_at = clock_timestamp()
                            + (%s * interval '1 second'),
                        updated_at = clock_timestamp()
                    WHERE command_id = %s AND lease_id = %s AND lease_owner = %s
                      AND status = 'processing'
                      AND lease_expires_at > clock_timestamp()
                    """,
                    (lease_seconds, command_id, lease_id, lease_owner),
                )
                return result.rowcount == 1
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to renew outbox lease") from error

    async def recover_expired_outbox_leases(
        self,
        *,
        batch_size: int,
    ) -> int:
        """Recover outbox messages whose lease expired; return recovered count."""
        recovered = 0
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            """
                            SELECT command_id, lease_id, attempts_in_cycle
                            FROM integration.outbox_messages
                            WHERE status = 'processing'
                              AND lease_expires_at < clock_timestamp()
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                            """,
                            (batch_size,),
                        )
                        rows = await cursor.fetchall()
                        for row in rows:
                            await cursor.execute(
                                """
                                UPDATE integration.outbox_delivery_attempts
                                SET finished_at = clock_timestamp(),
                                    outcome = 'lease_expired',
                                    safe_error_code = %s
                                WHERE command_id = %s AND lease_id = %s
                                  AND finished_at IS NULL
                                """,
                                (
                                    "lease_expired"
                                    if row["attempts_in_cycle"] < 8
                                    else "lease_expired_attempts_exhausted",
                                    row["command_id"],
                                    row["lease_id"],
                                ),
                            )
                            if row["attempts_in_cycle"] < 8:
                                # Attempts 1..7: the message may be retried.
                                await cursor.execute(
                                    """
                                    UPDATE integration.outbox_messages
                                    SET status = 'retry_scheduled',
                                        available_at = clock_timestamp(),
                                        lease_id = NULL, lease_owner = NULL,
                                        lease_expires_at = NULL,
                                        updated_at = clock_timestamp()
                                    WHERE command_id = %s
                                    """,
                                    (row["command_id"],),
                                )
                            else:
                                # The 8th attempt already consumed the cycle:
                                # the message goes straight to dead. No
                                # last_failure_kind is invented for a worker
                                # crash.
                                await cursor.execute(
                                    """
                                    UPDATE integration.outbox_messages
                                    SET status = 'dead',
                                        dead_at = clock_timestamp(),
                                        lease_id = NULL, lease_owner = NULL,
                                        lease_expires_at = NULL,
                                        last_error_code =
                                            'lease_expired_attempts_exhausted',
                                        updated_at = clock_timestamp()
                                    WHERE command_id = %s
                                    """,
                                    (row["command_id"],),
                                )
                            recovered += 1
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError(
                "Failed to recover expired outbox leases"
            ) from error
        return recovered

    async def schedule_outbox_retry(
        self,
        *,
        command_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        attempt_id: UUID,
        failure_kind: ProviderFailureKind,
        error_code: str | None,
        error_message: str | None,
        retry_after_seconds: float | None,
        next_available_at: datetime,
    ) -> None:
        """Finalize the attempt and schedule the next delivery in one transaction."""
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            """
                            SELECT status, lease_id, lease_owner,
                                   attempts_in_cycle
                            FROM integration.outbox_messages
                            WHERE command_id = %s
                            FOR UPDATE
                            """,
                            (command_id,),
                        )
                        message = await cursor.fetchone()
                        if message is None:
                            raise LeaseConflictError(str(command_id))
                        if message["status"] != "processing":
                            raise LeaseConflictError(str(command_id))
                        if message["lease_id"] != lease_id:
                            raise LeaseConflictError(str(command_id))
                        if message["lease_owner"] != lease_owner:
                            raise LeaseConflictError(str(command_id))
                        if message["attempts_in_cycle"] >= 8:
                            raise OutboxAttemptsExhaustedError(str(command_id))
                        await cursor.execute("SELECT clock_timestamp() AS now")
                        row = await cursor.fetchone()
                        if row is None:
                            raise IntegrationPersistenceError(
                                "clock_timestamp() returned no row"
                            )
                        now = row["now"]
                        affected_attempt = await finish_delivery_attempt(
                            cursor,
                            attempt_id=attempt_id,
                            command_id=command_id,
                            lease_id=lease_id,
                            worker_id=lease_owner,
                            finished_at=now,
                            outcome="retry_scheduled",
                            failure_kind=failure_kind,
                            safe_error_code=error_code,
                            safe_error_message=error_message,
                            retry_after_seconds=retry_after_seconds,
                            next_available_at=next_available_at,
                        )
                        if affected_attempt != 1:
                            raise LeaseConflictError(str(attempt_id))
                        affected = await update_outbox_transition(
                            cursor,
                            command_id=command_id,
                            expected_status="processing",
                            expected_lease_id=lease_id,
                            expected_lease_owner=lease_owner,
                            target_status="retry_scheduled",
                            updated_at=now,
                            available_at=next_available_at,
                            last_failure_kind=failure_kind,
                            last_error_code=error_code,
                            last_error_message=error_message,
                        )
                        if affected != 1:
                            raise LeaseConflictError(str(command_id))
        except (LeaseConflictError, OutboxAttemptsExhaustedError):
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to schedule outbox retry") from error

    async def redrive_dead_outbox(
        self,
        *,
        command_id: UUID,
        tenant_id: str,
        request_id: str,
        requested_by: str,
        reason: str,
        redrive_id: UUID,
        created_at: datetime,
    ) -> OutboxRedrive:
        """Manually redrive a dead outbox command into a fresh delivery cycle."""
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            """
                            SELECT delivery_cycle, status, tenant_id
                            FROM integration.outbox_messages
                            WHERE command_id = %s
                            FOR UPDATE
                            """,
                            (command_id,),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise OutboxMessageNotFoundError(str(command_id))
                        if row["tenant_id"] != tenant_id:
                            raise OutboxMessageNotFoundError(str(command_id))
                        # A duplicate request must be reported as such even
                        # after the first redrive moved the message out of
                        # dead; the unique constraint below remains as the
                        # race fallback for concurrent same-request redrives.
                        await cursor.execute(
                            """
                            SELECT 1
                            FROM integration.outbox_redrives
                            WHERE tenant_id = %s
                              AND request_id = %s
                            """,
                            (tenant_id, request_id),
                        )
                        if await cursor.fetchone() is not None:
                            raise DuplicateRedriveRequestError(request_id)
                        if row["status"] != "dead":
                            raise InvalidRedriveStateError(
                                f"outbox message is {row['status']}, not dead"
                            )
                        previous_cycle = int(row["delivery_cycle"])
                        new_cycle = previous_cycle + 1
                        await cursor.execute(
                            """
                            UPDATE integration.outbox_messages
                            SET status = 'retry_scheduled',
                                delivery_cycle = %s,
                                attempts_in_cycle = 0,
                                available_at = clock_timestamp(),
                                dead_at = NULL,
                                lease_id = NULL, lease_owner = NULL,
                                lease_expires_at = NULL,
                                updated_at = clock_timestamp()
                            WHERE command_id = %s
                            """,
                            (new_cycle, command_id),
                        )
                        await cursor.execute(
                            """
                            INSERT INTO integration.outbox_redrives (
                                redrive_id, command_id, tenant_id, request_id,
                                requested_by, reason, previous_cycle, new_cycle,
                                created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                redrive_id,
                                command_id,
                                tenant_id,
                                request_id,
                                requested_by,
                                reason,
                                previous_cycle,
                                new_cycle,
                                created_at,
                            ),
                        )
        except errors.UniqueViolation as error:
            if error.diag.constraint_name == "uq_outbox_redrives_tenant_request":
                raise DuplicateRedriveRequestError(request_id) from error
            raise IntegrationPersistenceError("Failed to redrive outbox message") from error
        except (
            OutboxMessageNotFoundError,
            InvalidRedriveStateError,
            DuplicateRedriveRequestError,
        ):
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to redrive outbox message") from error
        return OutboxRedrive(
            redrive_id=redrive_id,
            command_id=command_id,
            tenant_id=tenant_id,
            request_id=request_id,
            requested_by=requested_by,
            reason=reason,
            previous_cycle=previous_cycle,
            new_cycle=new_cycle,
            created_at=created_at,
        )

    # ------------------------------------------------------------- inbox

    async def get_inbox_message(self, inbox_id: UUID) -> InboxMessage | None:
        """Return one inbox message by id."""
        query = f"""
            SELECT {_INBOX_COLUMNS}
            FROM integration.inbox_messages
            WHERE inbox_id = %s
        """
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, (inbox_id,))
                    row = await cursor.fetchone()
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to read inbox message") from error
        return None if row is None else _inbox_from_row(row)

    async def receive_inbox_idempotently(
        self,
        *,
        inbox_id: UUID,
        provider_connection_id: str,
        event_id: str,
        tenant_id: str,
        event: ProviderWebhookEventData,
        raw_body_sha256: str,
        received_at: datetime,
    ) -> InboxMessage:
        """Store one verified webhook event; duplicates return the existing row.

        An exact replay (every trusted field and the canonical typed payload
        identical) returns the existing record; any difference raises
        ``InboxEventConflictError`` without touching the original row.
        """
        # psycopg cannot adapt a plain dict with %s: the Jsonb wrapper selects
        # the jsonb type adaptation so the server receives a valid JSONB value.
        payload = Jsonb(event.model_dump(mode="json"))
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            f"""
                            INSERT INTO integration.inbox_messages (
                                inbox_id, provider_connection_id, event_id,
                                tenant_id, schema_version, event_type,
                                command_id, aggregate_type, aggregate_id,
                                payload, raw_body_sha256, status,
                                processing_attempts, available_at,
                                received_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, 'received', 0, %s, %s, %s
                            )
                            ON CONFLICT (provider_connection_id, event_id)
                                DO NOTHING
                            RETURNING {_INBOX_COLUMNS}
                            """,
                            (
                                inbox_id,
                                provider_connection_id,
                                event_id,
                                tenant_id,
                                SCHEMA_VERSION,
                                "provider_command_status_changed",
                                event.command_id,
                                event.aggregate_type,
                                event.aggregate_id,
                                payload,
                                raw_body_sha256,
                                received_at,
                                received_at,
                                received_at,
                            ),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            await cursor.execute(
                                f"""
                                SELECT {_INBOX_COLUMNS}
                                FROM integration.inbox_messages
                                WHERE provider_connection_id = %s
                                  AND event_id = %s
                                """,
                                (provider_connection_id, event_id),
                            )
                            row = await cursor.fetchone()
                            if row is None:
                                raise IntegrationPersistenceError(
                                    "Inbox message was not persisted"
                                )
                            existing = _inbox_from_row(row)
                            _verify_inbox_exact_replay(
                                existing=existing,
                                provider_connection_id=provider_connection_id,
                                event_id=event_id,
                                tenant_id=tenant_id,
                                event=event,
                                raw_body_sha256=raw_body_sha256,
                            )
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError(
                "Failed to receive inbox message"
            ) from error
        if row is None:
            raise IntegrationPersistenceError("Inbox message was not persisted")
        return _inbox_from_row(row)

    async def claim_due_inbox(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: float,
    ) -> list[ClaimedInboxMessage]:
        """Claim due inbox messages with SKIP LOCKED and create attempts."""
        _validate_claim_parameters(
            worker_id=worker_id,
            batch_size=batch_size,
            lease_seconds=lease_seconds,
        )
        claimed: list[ClaimedInboxMessage] = []
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            f"""
                            SELECT {_INBOX_COLUMNS}
                            FROM integration.inbox_messages
                            WHERE inbox_id IN (
                                SELECT inbox_id FROM integration.inbox_messages
                                WHERE status = 'received'
                                  AND processing_attempts < 5
                                  AND available_at <= clock_timestamp()
                                ORDER BY available_at, received_at, inbox_id
                                FOR UPDATE SKIP LOCKED
                                LIMIT %s
                            )
                            ORDER BY available_at, received_at, inbox_id
                            """,
                            (batch_size,),
                        )
                        rows = await cursor.fetchall()
                        for row in rows:
                            lease_id = uuid4()
                            attempt_id = uuid4()
                            await cursor.execute(
                                f"""
                                UPDATE integration.inbox_messages
                                SET status = 'processing',
                                    lease_id = %s,
                                    lease_owner = %s,
                                    lease_expires_at = clock_timestamp()
                                        + (%s * interval '1 second'),
                                    processing_attempts = processing_attempts + 1,
                                    updated_at = clock_timestamp()
                                WHERE inbox_id = %s
                                  AND status = 'received'
                                  AND processing_attempts < 5
                                RETURNING {_INBOX_COLUMNS}
                                """,
                                (lease_id, worker_id, lease_seconds, row["inbox_id"]),
                            )
                            updated_row = await cursor.fetchone()
                            if cursor.rowcount != 1 or updated_row is None:
                                raise LeaseConflictError(str(row["inbox_id"]))
                            await cursor.execute(
                                """
                                INSERT INTO integration.inbox_processing_attempts (
                                    attempt_id, inbox_id, attempt_number,
                                    lease_id, worker_id, started_at
                                ) VALUES (%s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    attempt_id,
                                    updated_row["inbox_id"],
                                    updated_row["processing_attempts"],
                                    lease_id,
                                    worker_id,
                                    updated_row["updated_at"],
                                ),
                            )
                            attempt = InboxProcessingAttempt(
                                attempt_id=attempt_id,
                                inbox_id=updated_row["inbox_id"],
                                attempt_number=updated_row["processing_attempts"],
                                lease_id=lease_id,
                                worker_id=worker_id,
                                started_at=updated_row["updated_at"],
                            )
                            payload = ProviderWebhookEventData.model_validate(
                                updated_row["payload"]
                            )
                            claimed.append(
                                ClaimedInboxMessage.model_validate(
                                    {
                                        **updated_row,
                                        "payload": payload,
                                        "attempt": attempt,
                                    }
                                )
                            )
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to claim inbox messages") from error
        return claimed

    async def renew_inbox_lease(
        self,
        *,
        inbox_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        """Extend the inbox lease while the worker is still alive.

        Returns ``False`` when the lease already expired: a renew must never
        resurrect an expired lease.
        """
        _validate_claim_parameters(
            worker_id=lease_owner,
            batch_size=1,
            lease_seconds=lease_seconds,
        )
        try:
            async with self._pool.connection() as connection:
                result = await connection.execute(
                    """
                    UPDATE integration.inbox_messages
                    SET lease_expires_at = clock_timestamp()
                            + (%s * interval '1 second'),
                        updated_at = clock_timestamp()
                    WHERE inbox_id = %s AND lease_id = %s AND lease_owner = %s
                      AND status = 'processing'
                      AND lease_expires_at > clock_timestamp()
                    """,
                    (lease_seconds, inbox_id, lease_id, lease_owner),
                )
                return result.rowcount == 1
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to renew inbox lease") from error

    async def recover_expired_inbox_leases(
        self,
        *,
        batch_size: int,
    ) -> int:
        """Recover inbox messages whose lease expired; return recovered count."""
        recovered = 0
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute(
                            """
                            SELECT inbox_id, lease_id, lease_owner, processing_attempts
                            FROM integration.inbox_messages
                            WHERE status = 'processing'
                              AND lease_expires_at < clock_timestamp()
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                            """,
                            (batch_size,),
                        )
                        rows = await cursor.fetchall()
                        for row in rows:
                            await cursor.execute(
                                """
                                UPDATE integration.inbox_processing_attempts
                                SET finished_at = clock_timestamp(),
                                    outcome = 'lease_expired',
                                    safe_error_code = 'lease_expired'
                                WHERE inbox_id = %s AND lease_id = %s
                                  AND worker_id = %s AND finished_at IS NULL
                                """,
                                (row["inbox_id"], row["lease_id"], row["lease_owner"]),
                            )
                            if cursor.rowcount != 1:
                                raise LeaseConflictError(str(row["inbox_id"]))
                            if row["processing_attempts"] >= 5:
                                await cursor.execute(
                                    """
                                    UPDATE integration.inbox_messages
                                    SET status = 'failed', failed_at = clock_timestamp(),
                                        lease_id = NULL, lease_owner = NULL,
                                        lease_expires_at = NULL,
                                        last_error_code = 'lease_expired_attempts_exhausted',
                                        updated_at = clock_timestamp()
                                    WHERE inbox_id = %s AND status = 'processing'
                                      AND lease_id = %s AND lease_owner = %s
                                      AND lease_expires_at < clock_timestamp()
                                    """,
                                    (row["inbox_id"], row["lease_id"], row["lease_owner"]),
                                )
                            else:
                                await cursor.execute(
                                    """
                                    UPDATE integration.inbox_messages
                                    SET status = 'received',
                                    available_at = clock_timestamp(),
                                    lease_id = NULL, lease_owner = NULL,
                                    lease_expires_at = NULL,
                                    updated_at = clock_timestamp()
                                WHERE inbox_id = %s AND status = 'processing'
                                  AND lease_id = %s AND lease_owner = %s
                                  AND lease_expires_at < clock_timestamp()
                                """,
                                    (row["inbox_id"], row["lease_id"], row["lease_owner"]),
                                )
                            if cursor.rowcount != 1:
                                raise LeaseConflictError(str(row["inbox_id"]))
                            recovered += 1
        except LeaseConflictError:
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError(
                "Failed to recover expired inbox leases"
            ) from error
        return recovered

    async def mark_inbox_failed(
        self,
        *,
        inbox_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Mark a leased inbox message failed (terminal)."""
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(
                            """
                            SELECT attempt_id
                            FROM integration.inbox_processing_attempts
                            WHERE inbox_id = %s AND lease_id = %s
                              AND finished_at IS NULL
                            ORDER BY attempt_number DESC
                            LIMIT 1
                            """,
                            (inbox_id, lease_id),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise LeaseConflictError(str(inbox_id))
                        await cursor.execute("SELECT clock_timestamp() AS now")
                        now_row = await cursor.fetchone()
                        if now_row is None:
                            raise IntegrationPersistenceError(
                                "clock_timestamp() returned no row"
                            )
                        now = now_row[0]
                        affected = await finish_inbox_attempt(
                            cursor,
                            attempt_id=row[0],
                            inbox_id=inbox_id,
                            lease_id=lease_id,
                            worker_id=lease_owner,
                            finished_at=now,
                            outcome="terminal_failure",
                            safe_error_code=error_code,
                            safe_error_message=error_message,
                        )
                        if affected != 1:
                            raise LeaseConflictError(str(inbox_id))
                        affected = await mark_inbox_failed_cursor(
                            cursor,
                            inbox_id=inbox_id,
                            lease_id=lease_id,
                            lease_owner=lease_owner,
                            failed_at=now,
                            error_code=error_code,
                            error_message=error_message,
                        )
                        if affected != 1:
                            raise LeaseConflictError(str(inbox_id))
        except LeaseConflictError:
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to mark inbox message failed") from error

    async def schedule_inbox_retry(
        self, *, inbox_id: UUID, lease_id: UUID, lease_owner: str,
        attempt_id: UUID, next_available_at: datetime, error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Atomically complete a fenced attempt and return the Inbox to received."""
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        await cursor.execute("SELECT processing_attempts FROM integration.inbox_messages WHERE inbox_id = %s AND status = 'processing' AND lease_id = %s AND lease_owner = %s FOR UPDATE", (inbox_id, lease_id, lease_owner))
                        row = await cursor.fetchone()
                        if row is None:
                            raise LeaseConflictError(str(inbox_id))
                        await cursor.execute("SELECT clock_timestamp() AS now")
                        now_row = await cursor.fetchone()
                        if now_row is None:
                            raise IntegrationPersistenceError("clock_timestamp returned no row")
                        now = now_row["now"]
                        if row["processing_attempts"] >= 5:
                            if await finish_inbox_attempt(cursor, attempt_id=attempt_id, inbox_id=inbox_id, lease_id=lease_id, worker_id=lease_owner, finished_at=now, outcome="terminal_failure", safe_error_code="inbox_attempts_exhausted", safe_error_message="Inbox processing attempts were exhausted.") != 1:
                                raise LeaseConflictError(str(inbox_id))
                            if await mark_inbox_failed_cursor(cursor, inbox_id=inbox_id, lease_id=lease_id, lease_owner=lease_owner, failed_at=now, error_code="inbox_attempts_exhausted", error_message="Inbox processing attempts were exhausted.") != 1:
                                raise LeaseConflictError(str(inbox_id))
                            return
                        if await finish_inbox_attempt(cursor, attempt_id=attempt_id, inbox_id=inbox_id, lease_id=lease_id, worker_id=lease_owner, finished_at=now, outcome="retry_scheduled", safe_error_code=error_code, safe_error_message=error_message) != 1:
                            raise LeaseConflictError(str(inbox_id))
                        if await schedule_inbox_retry(cursor, inbox_id=inbox_id, lease_id=lease_id, lease_owner=lease_owner, available_at=next_available_at, updated_at=now, error_code=error_code, error_message=error_message) != 1:
                            raise LeaseConflictError(str(inbox_id))
        except (LeaseConflictError, InboxAttemptsExhaustedError):
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError("Failed to schedule inbox retry") from error
