"""Unit coverage for bounded Inbox Worker routing and safe batch handling."""

import asyncio
from datetime import UTC, datetime, timedelta
from math import inf, nan
from uuid import uuid4

import pytest

from agent.integrations.inbox_finalizer import InboxFinalizationResult
from agent.integrations.inbox_worker import InboxProcessingWorker, InboxWorkerRunResult
from agent.integrations.models import ProviderWebhookEventData
from agent.integrations.persistence_models import (
    ClaimedInboxMessage,
    InboxProcessingAttempt,
)
from agent.integrations.repository import LeaseConflictError

pytestmark = pytest.mark.anyio


def _claimed(
    aggregate_type: str,
    *,
    index: int = 1,
) -> ClaimedInboxMessage:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    inbox_id = uuid4()
    aggregate_id = uuid4()
    lease_id = uuid4()
    command_id = uuid4()
    event = ProviderWebhookEventData(
        command_id=command_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        command_status="accepted",
        provider_reference=f"reference-{index}",
        order_id=f"ORD-{index:05}",
        occurred_at=now,
    )
    return ClaimedInboxMessage(
        inbox_id=inbox_id,
        provider_connection_id="connection-1",
        event_id=f"event-{index}",
        tenant_id="tenant-a",
        event_type="provider_command_status_changed",
        command_id=command_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=event,
        raw_body_sha256="a" * 64,
        status="processing",
        processing_attempts=1,
        available_at=now,
        lease_id=lease_id,
        lease_owner="inbox-worker",
        lease_expires_at=now + timedelta(minutes=1),
        received_at=now,
        updated_at=now,
        attempt=InboxProcessingAttempt(
            attempt_id=uuid4(),
            inbox_id=inbox_id,
            attempt_number=1,
            lease_id=lease_id,
            worker_id="inbox-worker",
            started_at=now,
        ),
    )


class FakeRepository:
    def __init__(
        self,
        claimed: list[ClaimedInboxMessage],
        renewal_results: list[object] | None = None,
        claim_results: list[list[ClaimedInboxMessage]] | None = None,
        recovery_results: list[object] | None = None,
    ) -> None:
        self.claimed = claimed
        self.claim_results = claim_results or []
        self.claim_arguments: list[dict[str, object]] = []
        self.claim_started: asyncio.Queue[int] = asyncio.Queue()
        self.renewal_results = renewal_results or []
        self.renew_calls: list[dict[str, object]] = []
        self.renew_started: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.recovery_results = recovery_results or []
        self.recovery_arguments: list[dict[str, object]] = []
        self.call_log: list[str] = []

    async def claim_due_inbox(self, **kwargs):
        self.call_log.append("claim")
        self.claim_arguments.append(kwargs)
        await self.claim_started.put(len(self.claim_arguments))
        if self.claim_results:
            return self.claim_results.pop(0)
        return self.claimed

    async def recover_expired_inbox_leases(self, **kwargs) -> int:
        self.call_log.append("recover")
        self.recovery_arguments.append(kwargs)
        result = self.recovery_results.pop(0) if self.recovery_results else 0
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, int)
        return result

    async def renew_inbox_lease(self, **kwargs) -> bool:
        self.renew_calls.append(kwargs)
        await self.renew_started.put(kwargs)
        result = self.renewal_results.pop(0) if self.renewal_results else True
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result()
        assert isinstance(result, bool)
        return result


class FakeFinalizer:
    def __init__(self, behaviors: dict[object, object]) -> None:
        self.behaviors = behaviors
        self.calls: list[tuple[str, ClaimedInboxMessage, datetime]] = []

    async def finalize_order_operation(
        self,
        *,
        claimed: ClaimedInboxMessage,
        retry_available_at: datetime,
    ) -> InboxFinalizationResult:
        return await self._run("order_operation", claimed, retry_available_at)

    async def finalize_support_case(
        self,
        *,
        claimed: ClaimedInboxMessage,
        retry_available_at: datetime,
    ) -> InboxFinalizationResult:
        return await self._run("support_case", claimed, retry_available_at)

    async def _run(
        self,
        route: str,
        claimed: ClaimedInboxMessage,
        retry_available_at: datetime,
    ) -> InboxFinalizationResult:
        self.calls.append((route, claimed, retry_available_at))
        behavior = self.behaviors[claimed.inbox_id]
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return await behavior()
        assert isinstance(behavior, InboxFinalizationResult)
        return behavior


class ControlledSleep:
    def __init__(self, *, call_log: list[str] | None = None) -> None:
        self.calls: list[float] = []
        self.started: asyncio.Queue[asyncio.Event] = asyncio.Queue()
        self.cancelled = asyncio.Event()
        self._call_log = call_log

    async def __call__(self, seconds: float) -> None:
        release = asyncio.Event()
        self.calls.append(seconds)
        if self._call_log is not None:
            self._call_log.append(f"sleep:{seconds}")
        await self.started.put(release)
        try:
            await release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class ControlledMonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _result(claimed: ClaimedInboxMessage, action: str) -> InboxFinalizationResult:
    return InboxFinalizationResult(
        action=action,
        aggregate_type=claimed.aggregate_type,
    )


def _worker(
    repository: FakeRepository,
    finalizer: FakeFinalizer,
    *,
    clock: datetime | None = None,
    retry_delay_seconds: float = 5.0,
    lease_seconds: float = 60.0,
    sleep=None,
    monotonic_clock: ControlledMonotonicClock | None = None,
) -> InboxProcessingWorker:
    arguments = {
        "repository": repository,
        "finalizer": finalizer,
        "worker_id": "inbox-worker",
        "retry_delay_seconds": retry_delay_seconds,
        "lease_seconds": lease_seconds,
        "clock": (lambda: clock) if clock is not None else None,
        "monotonic_clock": monotonic_clock or ControlledMonotonicClock(),
    }
    if sleep is not None:
        arguments["sleep"] = sleep
    return InboxProcessingWorker(**arguments)


async def test_empty_queue_returns_zero_result_without_finalizer_calls() -> None:
    repository = FakeRepository([])
    finalizer = FakeFinalizer({})

    result = await _worker(repository, finalizer).run_once()

    assert result == InboxWorkerRunResult()
    assert finalizer.calls == []
    assert repository.claim_arguments == [
        {"worker_id": "inbox-worker", "batch_size": 20, "lease_seconds": 60.0}
    ]


async def test_routes_each_aggregate_with_fixed_retry_time() -> None:
    now = datetime(2026, 2, 1, tzinfo=UTC)
    order = _claimed("order_operation", index=1)
    support = _claimed("support_case", index=2)
    finalizer = FakeFinalizer(
        {
            order.inbox_id: _result(order, "applied"),
            support.inbox_id: _result(support, "duplicate"),
        }
    )

    result = await _worker(
        FakeRepository([order, support]),
        finalizer,
        clock=now,
        retry_delay_seconds=7.0,
    ).run_once()

    assert result == InboxWorkerRunResult(claimed=2, applied=1, duplicates=1)
    assert finalizer.calls == [
        ("order_operation", order, now + timedelta(seconds=7)),
        ("support_case", support, now + timedelta(seconds=7)),
    ]


@pytest.mark.parametrize(
    ("action", "counter"),
    [
        ("applied", "applied"),
        ("duplicate", "duplicates"),
        ("stale", "stale"),
        ("retry_scheduled", "retried"),
        ("failed", "failed"),
    ],
)
async def test_maps_all_finalization_actions_to_one_counter(
    action: str,
    counter: str,
) -> None:
    claimed = _claimed("support_case")
    result = await _worker(
        FakeRepository([claimed]),
        FakeFinalizer({claimed.inbox_id: _result(claimed, action)}),
    ).run_once()

    assert result.claimed == 1
    assert getattr(result, counter) == 1
    assert (
        sum(result.__dict__[name] for name in result.__dict__ if name != "claimed") == 1
    )


async def test_dispatches_a_batch_concurrently() -> None:
    first = _claimed("order_operation", index=1)
    second = _claimed("support_case", index=2)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_behavior() -> InboxFinalizationResult:
        first_started.set()
        await second_started.wait()
        await release_first.wait()
        return _result(first, "applied")

    async def second_behavior() -> InboxFinalizationResult:
        second_started.set()
        return _result(second, "duplicate")

    worker_task = asyncio.create_task(
        _worker(
            FakeRepository([first, second]),
            FakeFinalizer(
                {
                    first.inbox_id: first_behavior,
                    second.inbox_id: second_behavior,
                }
            ),
        ).run_once()
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await asyncio.wait_for(second_started.wait(), timeout=1)
    assert not worker_task.done()
    release_first.set()

    assert await worker_task == InboxWorkerRunResult(
        claimed=2,
        applied=1,
        duplicates=1,
    )


async def test_isolates_lease_conflicts_and_ordinary_errors() -> None:
    applied = _claimed("order_operation", index=1)
    conflict = _claimed("support_case", index=2)
    error = _claimed("order_operation", index=3)
    duplicate = _claimed("support_case", index=4)
    finalizer = FakeFinalizer(
        {
            applied.inbox_id: _result(applied, "applied"),
            conflict.inbox_id: LeaseConflictError("do-not-persist"),
            error.inbox_id: RuntimeError("credential=not-safe"),
            duplicate.inbox_id: _result(duplicate, "duplicate"),
        }
    )

    result = await _worker(
        FakeRepository([applied, conflict, error, duplicate]),
        finalizer,
    ).run_once()

    assert result == InboxWorkerRunResult(
        claimed=4,
        applied=1,
        duplicates=1,
        lease_conflicts=1,
        errors=1,
    )
    assert "credential" not in repr(result)
    assert "do-not-persist" not in repr(result)


async def test_aggregate_type_mismatch_isolated_as_worker_error() -> None:
    mismatched = _claimed("order_operation", index=1)
    sibling = _claimed("support_case", index=2)
    finalizer = FakeFinalizer(
        {
            mismatched.inbox_id: InboxFinalizationResult(
                action="applied",
                aggregate_type="support_case",
            ),
            sibling.inbox_id: _result(sibling, "applied"),
        }
    )

    result = await _worker(
        FakeRepository([mismatched, sibling]),
        finalizer,
    ).run_once()

    assert result == InboxWorkerRunResult(claimed=2, applied=1, errors=1)


async def test_cancellation_cancels_and_reaps_sibling_tasks() -> None:
    blocking = _claimed("order_operation", index=1)
    cancelling = _claimed("support_case", index=2)
    blocking_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        blocking_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()
        return _result(blocking, "applied")

    async def cancellation_behavior() -> InboxFinalizationResult:
        await blocking_started.wait()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            _worker(
                FakeRepository([blocking, cancelling]),
                FakeFinalizer(
                    {
                        blocking.inbox_id: blocking_behavior,
                        cancelling.inbox_id: cancellation_behavior,
                    }
                ),
            ).run_once(),
            timeout=1,
        )

    assert sibling_cancelled.is_set()


async def test_fast_finalizer_completes_before_first_renewal() -> None:
    claimed = _claimed("support_case")
    repository = FakeRepository([claimed])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finish_finalizer = asyncio.Event()

    async def fast_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        await finish_finalizer.wait()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: fast_behavior}),
            sleep=timer,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    await asyncio.wait_for(timer.started.get(), timeout=1)
    finish_finalizer.set()

    assert await worker_task == InboxWorkerRunResult(claimed=1, applied=1)
    assert repository.renew_calls == []
    await asyncio.wait_for(timer.cancelled.wait(), timeout=1)


async def test_slow_finalizer_renews_once_then_completes() -> None:
    claimed = _claimed("order_operation")
    repository = FakeRepository([claimed], renewal_results=[True])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finish_finalizer = asyncio.Event()
    calls = 0

    async def slow_behavior() -> InboxFinalizationResult:
        nonlocal calls
        calls += 1
        finalizer_started.set()
        await finish_finalizer.wait()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: slow_behavior}),
            sleep=timer,
            lease_seconds=30.0,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    first_timer = await asyncio.wait_for(timer.started.get(), timeout=1)
    first_timer.set()
    renewal = await asyncio.wait_for(repository.renew_started.get(), timeout=1)
    assert renewal == {
        "inbox_id": claimed.inbox_id,
        "lease_id": claimed.lease_id,
        "lease_owner": claimed.lease_owner,
        "lease_seconds": 30.0,
    }
    await asyncio.wait_for(timer.started.get(), timeout=1)
    finish_finalizer.set()

    assert await worker_task == InboxWorkerRunResult(claimed=1, applied=1)
    assert calls == 1
    assert timer.calls == [10.0, 10.0]
    await asyncio.wait_for(timer.cancelled.wait(), timeout=1)


async def test_slow_finalizer_can_complete_multiple_renewals() -> None:
    claimed = _claimed("support_case")
    repository = FakeRepository([claimed], renewal_results=[True, True])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finish_finalizer = asyncio.Event()
    calls = 0

    async def slow_behavior() -> InboxFinalizationResult:
        nonlocal calls
        calls += 1
        finalizer_started.set()
        await finish_finalizer.wait()
        return _result(claimed, "duplicate")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: slow_behavior}),
            sleep=timer,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    for _ in range(2):
        renewal_timer = await asyncio.wait_for(timer.started.get(), timeout=1)
        renewal_timer.set()
        renewal = await asyncio.wait_for(repository.renew_started.get(), timeout=1)
        assert renewal["inbox_id"] == claimed.inbox_id
        assert renewal["lease_id"] == claimed.lease_id
        assert renewal["lease_owner"] == claimed.lease_owner
        assert renewal["lease_seconds"] == 60.0
    await asyncio.wait_for(timer.started.get(), timeout=1)
    finish_finalizer.set()

    assert await worker_task == InboxWorkerRunResult(claimed=1, duplicates=1)
    assert calls == 1
    assert len(repository.renew_calls) == 2
    await asyncio.wait_for(timer.cancelled.wait(), timeout=1)


async def test_failed_renewal_cancels_finalizer_and_counts_lease_conflict() -> None:
    claimed = _claimed("order_operation")
    repository = FakeRepository([claimed], renewal_results=[False])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finalizer_cancelled = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizer_cancelled.set()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: blocking_behavior}),
            sleep=timer,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    renewal_timer = await asyncio.wait_for(timer.started.get(), timeout=1)
    renewal_timer.set()

    assert await worker_task == InboxWorkerRunResult(claimed=1, lease_conflicts=1)
    assert finalizer_cancelled.is_set()
    assert len(repository.renew_calls) == 1


async def test_completed_finalizer_wins_a_race_with_false_renewal() -> None:
    claimed = _claimed("support_case")
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finalizer_finished = asyncio.Event()
    finish_finalizer = asyncio.Event()

    async def race_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        await finish_finalizer.wait()
        finalizer_finished.set()
        return _result(claimed, "applied")

    async def false_after_finalizer() -> bool:
        await finalizer_finished.wait()
        return False

    repository = FakeRepository([claimed], renewal_results=[false_after_finalizer])
    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: race_behavior}),
            sleep=timer,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    renewal_timer = await asyncio.wait_for(timer.started.get(), timeout=1)
    renewal_timer.set()
    await asyncio.wait_for(repository.renew_started.get(), timeout=1)
    finish_finalizer.set()

    assert await worker_task == InboxWorkerRunResult(claimed=1, applied=1)
    assert len(repository.renew_calls) == 1


async def test_renewal_exception_cancels_finalizer_and_counts_error() -> None:
    claimed = _claimed("order_operation")
    repository = FakeRepository([claimed], renewal_results=[RuntimeError("secret")])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finalizer_cancelled = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizer_cancelled.set()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: blocking_behavior}),
            sleep=timer,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    renewal_timer = await asyncio.wait_for(timer.started.get(), timeout=1)
    renewal_timer.set()

    result = await worker_task
    assert result == InboxWorkerRunResult(claimed=1, errors=1)
    assert finalizer_cancelled.is_set()
    assert "secret" not in repr(result)


async def test_outer_cancellation_reaps_finalizer_and_renewal_timer() -> None:
    claimed = _claimed("support_case")
    repository = FakeRepository([claimed])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finalizer_cancelled = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizer_cancelled.set()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: blocking_behavior}),
            sleep=timer,
        ).run_once()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    await asyncio.wait_for(timer.started.get(), timeout=1)
    worker_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await worker_task

    assert finalizer_cancelled.is_set()
    assert timer.cancelled.is_set()


async def test_run_forever_recovers_before_first_claim_and_polls_empty_queue() -> None:
    repository = FakeRepository([], recovery_results=[4])
    timer = ControlledSleep()
    worker_task = asyncio.create_task(
        _worker(repository, FakeFinalizer({}), sleep=timer).run_forever(
            poll_seconds=2.0
        )
    )

    await asyncio.wait_for(timer.started.get(), timeout=1)

    assert repository.call_log[:2] == ["recover", "claim"]
    assert repository.recovery_arguments == [{"batch_size": 20}]
    assert repository.claim_arguments == [
        {"worker_id": "inbox-worker", "batch_size": 20, "lease_seconds": 60.0}
    ]

    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task
    assert timer.cancelled.is_set()


async def test_run_forever_polls_empty_queue_then_claims_again() -> None:
    repository = FakeRepository([])
    timer = ControlledSleep()
    worker_task = asyncio.create_task(
        _worker(repository, FakeFinalizer({}), sleep=timer).run_forever(
            poll_seconds=2.0
        )
    )

    first_poll = await asyncio.wait_for(timer.started.get(), timeout=1)
    first_poll.set()
    await asyncio.wait_for(repository.claim_started.get(), timeout=1)
    await asyncio.wait_for(repository.claim_started.get(), timeout=1)

    assert len(repository.recovery_arguments) == 1
    assert timer.calls[:1] == [2.0]

    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task


async def test_run_forever_drains_claimed_batch_without_idle_poll() -> None:
    claimed = _claimed("order_operation")
    repository = FakeRepository(
        [],
        claim_results=[[claimed], []],
    )
    timer = ControlledSleep(call_log=repository.call_log)
    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: _result(claimed, "applied")}),
            sleep=timer,
        ).run_forever()
    )

    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 1
    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 2

    second_claim = repository.call_log.index("claim", 2)
    assert "sleep:1.0" not in repository.call_log[:second_claim]

    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task


@pytest.mark.parametrize(
    "behavior",
    [
        RuntimeError("safe test error"),
        LeaseConflictError("safe test conflict"),
    ],
)
async def test_run_forever_drains_claimed_failure_batches_without_idle_poll(
    behavior: BaseException,
) -> None:
    claimed = _claimed("support_case")
    repository = FakeRepository(
        [],
        claim_results=[[claimed], []],
    )
    timer = ControlledSleep(call_log=repository.call_log)
    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: behavior}),
            sleep=timer,
        ).run_forever()
    )

    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 1
    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 2
    second_claim = repository.call_log.index("claim", 2)
    assert "sleep:1.0" not in repository.call_log[:second_claim]

    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task


async def test_run_forever_recovers_on_deadline_during_active_drain() -> None:
    claimed = _claimed("order_operation")
    repository = FakeRepository(
        [],
        claim_results=[[claimed], []],
    )
    timer = ControlledSleep()
    monotonic_clock = ControlledMonotonicClock()
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        await release_finalizer.wait()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: blocking_behavior}),
            sleep=timer,
            monotonic_clock=monotonic_clock,
        ).run_forever(recovery_seconds=30.0)
    )
    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 1
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    monotonic_clock.value = 30.0
    release_finalizer.set()

    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 2
    assert repository.call_log[:4] == ["recover", "claim", "recover", "claim"]
    assert len(repository.recovery_arguments) == 2

    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task


async def test_run_forever_does_not_recover_before_deadline() -> None:
    claimed = _claimed("support_case")
    repository = FakeRepository(
        [],
        claim_results=[[claimed], []],
    )
    timer = ControlledSleep()
    monotonic_clock = ControlledMonotonicClock()
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        await release_finalizer.wait()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: blocking_behavior}),
            sleep=timer,
            monotonic_clock=monotonic_clock,
        ).run_forever(recovery_seconds=30.0)
    )
    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 1
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    monotonic_clock.value = 29.0
    release_finalizer.set()

    assert await asyncio.wait_for(repository.claim_started.get(), timeout=1) == 2
    assert repository.call_log[:3] == ["recover", "claim", "claim"]
    assert len(repository.recovery_arguments) == 1

    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task


async def test_run_forever_propagates_recovery_error_before_claiming() -> None:
    repository = FakeRepository([], recovery_results=[RuntimeError("recovery failed")])
    timer = ControlledSleep()

    with pytest.raises(RuntimeError, match="recovery failed"):
        await _worker(repository, FakeFinalizer({}), sleep=timer).run_forever()

    assert repository.claim_arguments == []
    assert timer.calls == []


async def test_run_forever_cancellation_during_idle_poll_is_transparent() -> None:
    repository = FakeRepository([])
    timer = ControlledSleep()
    worker_task = asyncio.create_task(
        _worker(repository, FakeFinalizer({}), sleep=timer).run_forever()
    )
    await asyncio.wait_for(timer.started.get(), timeout=1)
    worker_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await worker_task

    assert len(repository.recovery_arguments) == 1
    assert len(repository.claim_arguments) == 1
    assert timer.cancelled.is_set()


async def test_run_forever_cancellation_during_active_batch_reaps_tasks() -> None:
    claimed = _claimed("order_operation")
    repository = FakeRepository([], claim_results=[[claimed]])
    timer = ControlledSleep()
    finalizer_started = asyncio.Event()
    finalizer_cancelled = asyncio.Event()

    async def blocking_behavior() -> InboxFinalizationResult:
        finalizer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizer_cancelled.set()
        return _result(claimed, "applied")

    worker_task = asyncio.create_task(
        _worker(
            repository,
            FakeFinalizer({claimed.inbox_id: blocking_behavior}),
            sleep=timer,
        ).run_forever()
    )
    await asyncio.wait_for(finalizer_started.wait(), timeout=1)
    await asyncio.wait_for(timer.started.get(), timeout=1)
    worker_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await worker_task

    assert finalizer_cancelled.is_set()
    assert timer.cancelled.is_set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_seconds": value, "recovery_seconds": 1.0}
        for value in (0.0, -1.0, nan, inf, -inf)
    ]
    + [
        {"poll_seconds": 1.0, "recovery_seconds": value}
        for value in (0.0, -1.0, nan, inf, -inf)
    ],
)
async def test_run_forever_rejects_invalid_poll_and_recovery_intervals(
    kwargs: dict[str, float],
) -> None:
    repository = FakeRepository([])
    timer = ControlledSleep()

    with pytest.raises(ValueError, match="finite positive number"):
        await _worker(repository, FakeFinalizer({}), sleep=timer).run_forever(**kwargs)

    assert repository.recovery_arguments == []
    assert repository.claim_arguments == []
    assert timer.calls == []


@pytest.mark.parametrize("retry_delay_seconds", [0.0, -1.0, nan])
def test_rejects_invalid_worker_configuration(retry_delay_seconds: float) -> None:
    with pytest.raises(ValueError):
        _worker(
            FakeRepository([]),
            FakeFinalizer({}),
            retry_delay_seconds=retry_delay_seconds,
        )
    with pytest.raises(ValueError):
        InboxProcessingWorker(
            repository=FakeRepository([]),
            finalizer=FakeFinalizer({}),
            worker_id=" ",
        )
