"""Deterministic outbox dispatch scheduling independent of HTTP and Graph."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import random
from time import monotonic
from typing import Any

from agent.integrations.models import ProviderCommandResult
from agent.integrations.persistence_models import ClaimedOutboxMessage
from agent.integrations.provider import (
    ProviderCommandTransport,
    ProviderConnectionLookup,
    ProviderConnectionNotFoundError,
)
from agent.integrations.rate_limit import ConnectionGateRegistry
from agent.integrations.repository import (
    IntegrationRepository,
    LeaseConflictError,
    OutboxAttemptsExhaustedError,
)
from agent.integrations.retry import (
    HTTPStatusError,
    ProviderConnectionError,
    ProviderTimeoutError,
    classify_failure,
    decide_retry,
)


@dataclass(frozen=True)
class WorkerRunResult:
    """Summarize one bounded dispatch iteration without exposing payloads."""

    claimed: int = 0
    published: int = 0
    retried: int = 0
    dead: int = 0
    lease_conflicts: int = 0
    failed: int = 0


class OutboxFinalizer:
    """Finalize a claimed command with its aggregate in one database transaction."""

    async def accepted(
        self, *, claimed: ClaimedOutboxMessage, result: ProviderCommandResult
    ) -> None:
        """Persist an accepted command and its aggregate transition."""
        raise NotImplementedError

    async def rejected(
        self, *, claimed: ClaimedOutboxMessage, result: ProviderCommandResult
    ) -> None:
        """Persist an explicit provider business rejection."""
        raise NotImplementedError

    async def terminal_failure(
        self,
        *,
        claimed: ClaimedOutboxMessage,
        failure_kind: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Persist a non-retryable technical failure and required handoff."""
        raise NotImplementedError


class OutboxDispatchWorker:
    """Claim and dispatch one batch while keeping scheduling separate from policy."""

    def __init__(
        self,
        *,
        repository: IntegrationRepository,
        connection_lookup: ProviderConnectionLookup,
        transport: ProviderCommandTransport,
        finalizer: OutboxFinalizer,
        worker_id: str,
        gate_registry: ConnectionGateRegistry | None = None,
        batch_size: int = 20,
        lease_seconds: float = 90.0,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
        random_source: Callable[[], float] = random,
    ) -> None:
        """Create one independently runnable worker instance."""
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        self._repository = repository
        self._connection_lookup = connection_lookup
        self._transport = transport
        self._finalizer = finalizer
        self._worker_id = worker_id
        self._gates = gate_registry or ConnectionGateRegistry()
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock
        self._sleep = sleep
        self._random_source = random_source

    async def run_once(self) -> WorkerRunResult:
        """Process one claimed batch; one command failure never aborts siblings."""
        claimed = await self._repository.claim_due_outbox(
            worker_id=self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        results = await self._gather_batch(claimed)
        result = WorkerRunResult(claimed=len(claimed))
        # Merge in claim order, rather than completion order, for deterministic
        # counters and test output.
        for item_result in results:
            result = _merge(result, item_result)
        return result

    async def _gather_batch(
        self, claimed: tuple[ClaimedOutboxMessage, ...] | list[ClaimedOutboxMessage]
    ) -> list[WorkerRunResult]:
        tasks = [asyncio.create_task(self._process(item)) for item in claimed]
        try:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results: list[WorkerRunResult] = []
        for outcome in gathered:
            if isinstance(outcome, BaseException):
                # _process isolates ordinary failures. A BaseException here is
                # deliberately re-raised so cancellation is never swallowed.
                raise outcome
            results.append(outcome)
        return results

    async def run_forever(
        self,
        *,
        poll_seconds: float = 1.0,
        recovery_seconds: float = 30.0,
    ) -> None:
        """Run bounded batches until cancelled, periodically recovering leases."""
        next_recovery = self._monotonic_clock()
        while True:
            if self._monotonic_clock() >= next_recovery:
                await self._repository.recover_expired_outbox_leases(
                    batch_size=self._batch_size
                )
                next_recovery = self._monotonic_clock() + recovery_seconds
            result = await self.run_once()
            if result.claimed == 0:
                await self._sleep(poll_seconds)

    async def _process(
        self,
        claimed: ClaimedOutboxMessage,
    ) -> WorkerRunResult:
        """Dispatch one lease-owner command and apply deterministic failure policy."""
        try:
            command = claimed.to_envelope()
            assert claimed.lease_id is not None and claimed.lease_owner is not None
            connection = await self._connection_lookup.resolve_by_connection_id(
                connection_id=command.connection_id,
                capability=claimed.provider_capability,
            )
            if (
                connection.connection_id != command.connection_id
                or connection.tenant_id != command.tenant_id
                or connection.capability != claimed.provider_capability
            ):
                raise ValueError("provider connection did not match persisted command")
            response = await self._send_with_lease_renewal(
                claimed=claimed,
                operation=self._send_through_gate(
                    connection=connection,
                    command=command,
                ),
            )
            if response.status == "accepted":
                await self._finalizer.accepted(claimed=claimed, result=response)
                return WorkerRunResult(published=1)
            await self._finalizer.rejected(claimed=claimed, result=response)
            return WorkerRunResult(dead=1)
        except LeaseConflictError:
            return WorkerRunResult(lease_conflicts=1)
        except Exception as error:
            return await self._handle_failure(claimed, error)

    async def _send_through_gate(self, *, connection, command) -> ProviderCommandResult:
        async with self._gates.for_connection(connection).acquire():
            return await self._transport.send_command(connection=connection, command=command)

    async def _send_with_lease_renewal(
        self,
        *,
        claimed: ClaimedOutboxMessage,
        operation: Coroutine[Any, Any, ProviderCommandResult],
    ) -> ProviderCommandResult:
        """Renew the fenced lease while a slow provider request is in flight."""
        assert claimed.lease_id is not None and claimed.lease_owner is not None
        request_task: asyncio.Task[ProviderCommandResult] = asyncio.create_task(operation)
        renewal_interval = max(self._lease_seconds / 3.0, 0.1)
        renewal_task: asyncio.Task[None] = asyncio.create_task(
            self._sleep(renewal_interval)
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {request_task, renewal_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if request_task in done:
                    return request_task.result()
                renewal_task.result()
                renewed = await self._repository.renew_outbox_lease(
                    command_id=claimed.command_id,
                    lease_id=claimed.lease_id,
                    lease_owner=claimed.lease_owner,
                    lease_seconds=self._lease_seconds,
                )
                if not renewed:
                    request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    raise LeaseConflictError(str(claimed.command_id))
                renewal_task = asyncio.create_task(self._sleep(renewal_interval))
        except BaseException:
            if not request_task.done():
                request_task.cancel()
            if not renewal_task.done():
                renewal_task.cancel()
            await asyncio.gather(request_task, renewal_task, return_exceptions=True)
            raise
        finally:
            if not renewal_task.done():
                renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)

    async def _handle_failure(
        self,
        claimed: ClaimedOutboxMessage,
        error: Exception,
    ) -> WorkerRunResult:
        """Schedule only explicitly classified retryable failures."""
        assert claimed.lease_id is not None and claimed.lease_owner is not None
        try:
            kind = classify_failure(error)
        except ValueError:
            kind = "validation_error"
        decision = decide_retry(
            kind=kind,
            attempts_so_far=claimed.attempt.attempt_number,
            retry_after_seconds=getattr(error, "retry_after_seconds", None),
            random_source=self._random_source,
        )
        safe_code, safe_message = _safe_error(error)
        try:
            if decision.retryable and decision.delay_seconds is not None:
                await self._repository.schedule_outbox_retry(
                    command_id=claimed.command_id,
                    lease_id=claimed.lease_id,
                    lease_owner=claimed.lease_owner,
                    attempt_id=claimed.attempt.attempt_id,
                    failure_kind=kind,
                    error_code=safe_code,
                    error_message=safe_message,
                    retry_after_seconds=decision.retry_after_seconds,
                    next_available_at=self._clock() + timedelta(seconds=decision.delay_seconds),
                )
                return WorkerRunResult(retried=1)
        except OutboxAttemptsExhaustedError:
            pass
        except LeaseConflictError:
            return WorkerRunResult(lease_conflicts=1)
        try:
            await self._finalizer.terminal_failure(
                claimed=claimed,
                failure_kind=kind,
                error_code=safe_code,
                error_message=safe_message,
            )
            return WorkerRunResult(dead=1)
        except LeaseConflictError:
            return WorkerRunResult(lease_conflicts=1)
        except Exception:
            return WorkerRunResult(failed=1)


def _increment(result: WorkerRunResult, field: str) -> WorkerRunResult:
    """Return one immutable result with the selected counter incremented."""
    values = result.__dict__.copy()
    values[field] += 1
    return WorkerRunResult(**values)


def _merge(left: WorkerRunResult, right: WorkerRunResult) -> WorkerRunResult:
    return WorkerRunResult(
        claimed=left.claimed,
        published=left.published + right.published,
        retried=left.retried + right.retried,
        dead=left.dead + right.dead,
        lease_conflicts=left.lease_conflicts + right.lease_conflicts,
        failed=left.failed + right.failed,
    )


def _safe_error(error: Exception) -> tuple[str, str]:
    """Map only known failures to fixed database-safe diagnostics."""
    if isinstance(error, ProviderConnectionNotFoundError):
        return "provider_connection_not_found", "Provider connection is unavailable."
    if isinstance(error, ProviderConnectionError):
        return "provider_connection_error", "Provider connection failed."
    if isinstance(error, ProviderTimeoutError):
        return "provider_timeout", "Provider request timed out."
    if isinstance(error, HTTPStatusError):
        return f"provider_http_{error.status_code}", "Provider returned an HTTP error."
    if isinstance(error, ValueError):
        return "provider_validation_error", "Provider command or response validation failed."
    return "unexpected_provider_error", "Provider command delivery failed unexpectedly."
