"""Bounded, database-agnostic scheduling for claimed provider Inbox messages."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from time import monotonic
from typing import Any

from agent.integrations.inbox_finalizer import InboxFinalizationResult, InboxFinalizer
from agent.integrations.persistence_models import ClaimedInboxMessage
from agent.integrations.repository import IntegrationRepository, LeaseConflictError


@dataclass(frozen=True)
class InboxWorkerRunResult:
    """Summarize one bounded Inbox processing run without business data."""

    claimed: int = 0
    applied: int = 0
    duplicates: int = 0
    stale: int = 0
    retried: int = 0
    failed: int = 0
    lease_conflicts: int = 0
    errors: int = 0


class InboxProcessingWorker:
    """Claim and route Inbox messages while finalizers retain all write authority."""

    def __init__(
        self,
        *,
        repository: IntegrationRepository,
        finalizer: InboxFinalizer,
        worker_id: str,
        batch_size: int = 20,
        lease_seconds: float = 60.0,
        retry_delay_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> None:
        """Create a bounded Inbox worker with an injectable retry-time clock."""
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if not isfinite(retry_delay_seconds) or retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds must be a finite positive number")
        self._repository = repository
        self._finalizer = finalizer
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock
        self._sleep = sleep

    async def run_once(self) -> InboxWorkerRunResult:
        """Process one claimed batch, isolating ordinary item failures."""
        claimed = await self._repository.claim_due_inbox(
            worker_id=self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        item_results = await self._gather_batch(claimed)
        result = InboxWorkerRunResult(claimed=len(claimed))
        for item_result in item_results:
            result = _merge(result, item_result)
        return result

    async def run_forever(
        self,
        *,
        poll_seconds: float = 1.0,
        recovery_seconds: float = 30.0,
    ) -> None:
        """Run bounded Inbox batches until cancelled, periodically recovering leases."""
        if not isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a finite positive number")
        if not isfinite(recovery_seconds) or recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be a finite positive number")

        await self._repository.recover_expired_inbox_leases(batch_size=self._batch_size)
        next_recovery = self._monotonic_clock() + recovery_seconds
        while True:
            if self._monotonic_clock() >= next_recovery:
                await self._repository.recover_expired_inbox_leases(
                    batch_size=self._batch_size
                )
                next_recovery = self._monotonic_clock() + recovery_seconds
            result = await self.run_once()
            if result.claimed == 0:
                await self._sleep(poll_seconds)

    async def _gather_batch(
        self,
        claimed: tuple[ClaimedInboxMessage, ...] | list[ClaimedInboxMessage],
    ) -> list[InboxWorkerRunResult]:
        tasks = [asyncio.create_task(self._process(item)) for item in claimed]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _process(self, claimed: ClaimedInboxMessage) -> InboxWorkerRunResult:
        """Route one claimed Inbox to its aggregate's atomic finalizer."""
        retry_available_at = self._clock() + timedelta(
            seconds=self._retry_delay_seconds
        )
        try:
            if claimed.aggregate_type == "order_operation":
                operation = self._finalizer.finalize_order_operation(
                    claimed=claimed, retry_available_at=retry_available_at
                )
            else:
                operation = self._finalizer.finalize_support_case(
                    claimed=claimed, retry_available_at=retry_available_at
                )
            result = await self._finalize_with_lease_renewal(
                claimed=claimed,
                operation=operation,
            )
        except LeaseConflictError:
            return InboxWorkerRunResult(lease_conflicts=1)
        except Exception:
            return InboxWorkerRunResult(errors=1)
        return _result_counter(claimed, result)

    async def _finalize_with_lease_renewal(
        self,
        *,
        claimed: ClaimedInboxMessage,
        operation: Coroutine[Any, Any, InboxFinalizationResult],
    ) -> InboxFinalizationResult:
        """Renew one fenced Inbox lease until its Finalizer completes or loses it."""
        assert claimed.lease_id is not None and claimed.lease_owner is not None
        finalizer_task: asyncio.Task[InboxFinalizationResult] = asyncio.create_task(
            operation
        )
        renewal_interval = max(self._lease_seconds / 3.0, 0.1)
        renewal_task: asyncio.Task[None] = asyncio.create_task(
            self._sleep(renewal_interval)
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {finalizer_task, renewal_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if finalizer_task in done:
                    return finalizer_task.result()
                renewal_task.result()
                renewed = await self._repository.renew_inbox_lease(
                    inbox_id=claimed.inbox_id,
                    lease_id=claimed.lease_id,
                    lease_owner=claimed.lease_owner,
                    lease_seconds=self._lease_seconds,
                )
                if not renewed:
                    # A Finalizer can commit and clear the lease while renewal is
                    # in flight. Its completed result is authoritative in that race.
                    if finalizer_task.done():
                        return finalizer_task.result()
                    finalizer_task.cancel()
                    await asyncio.gather(finalizer_task, return_exceptions=True)
                    raise LeaseConflictError(str(claimed.inbox_id))
                renewal_task = asyncio.create_task(self._sleep(renewal_interval))
        except BaseException:
            if not finalizer_task.done():
                finalizer_task.cancel()
            if not renewal_task.done():
                renewal_task.cancel()
            await asyncio.gather(
                finalizer_task,
                renewal_task,
                return_exceptions=True,
            )
            raise
        finally:
            if not renewal_task.done():
                renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)


def _result_counter(
    claimed: ClaimedInboxMessage,
    result: InboxFinalizationResult,
) -> InboxWorkerRunResult:
    """Map one validated Finalizer summary to exactly one safe result counter."""
    if result.aggregate_type != claimed.aggregate_type:
        return InboxWorkerRunResult(errors=1)
    counters = {
        "applied": "applied",
        "duplicate": "duplicates",
        "stale": "stale",
        "retry_scheduled": "retried",
        "failed": "failed",
    }
    field = counters.get(result.action)
    if field is None:
        return InboxWorkerRunResult(errors=1)
    return InboxWorkerRunResult(**{field: 1})


def _merge(
    left: InboxWorkerRunResult,
    right: InboxWorkerRunResult,
) -> InboxWorkerRunResult:
    """Merge item results in claim order without carrying untrusted details."""
    return InboxWorkerRunResult(
        claimed=left.claimed,
        applied=left.applied + right.applied,
        duplicates=left.duplicates + right.duplicates,
        stale=left.stale + right.stale,
        retried=left.retried + right.retried,
        failed=left.failed + right.failed,
        lease_conflicts=left.lease_conflicts + right.lease_conflicts,
        errors=left.errors + right.errors,
    )
