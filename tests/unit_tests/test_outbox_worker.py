"""Unit tests for deterministic worker scheduling and outcome mapping."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent.integrations.models import (
    OrderOperationCommandPayload,
    ProviderAuthentication,
    ProviderCommandResult,
    ProviderConnection,
)
from agent.integrations.outbox_worker import (
    OutboxDispatchWorker,
    OutboxFinalizer,
    WorkerRunResult,
)
from agent.integrations.persistence_models import (
    ClaimedOutboxMessage,
    OutboxDeliveryAttempt,
)
from agent.integrations.retry import ProviderConnectionError

pytestmark = pytest.mark.anyio


def _claimed() -> ClaimedOutboxMessage:
    now = datetime.now(UTC)
    command_id, aggregate_id, lease_id, attempt_id = uuid4(), uuid4(), uuid4(), uuid4()
    return ClaimedOutboxMessage(
        command_id=command_id, idempotency_key=f"order-operation:{aggregate_id}", tenant_id="tenant-demo", customer_id="customer-a",
        source_message_id="message-1", provider_connection_id="provider-demo", provider_capability="order_operation",
        command_type="return_order", aggregate_type="order_operation", aggregate_id=aggregate_id, expected_order_version=1,
        payload=OrderOperationCommandPayload(order_id="ORD-10001", operation_type="return", reason="damaged_item"),
        status="processing", delivery_cycle=1, attempts_in_cycle=1, available_at=now, lease_id=lease_id, lease_owner="worker-1",
        lease_expires_at=now + timedelta(seconds=90), created_at=now, updated_at=now,
        attempt=OutboxDeliveryAttempt(attempt_id=attempt_id, command_id=command_id, delivery_cycle=1, attempt_number=1, lease_id=lease_id, worker_id="worker-1", started_at=now),
    )


class Repository:
    def __init__(self, claimed: list[ClaimedOutboxMessage]) -> None:
        self.claimed = claimed
        self.retries = 0
    async def claim_due_outbox(self, **_kwargs): return self.claimed
    async def recover_expired_outbox_leases(self, **_kwargs): return 0
    async def renew_outbox_lease(self, **_kwargs): return True
    async def schedule_outbox_retry(self, **_kwargs): self.retries += 1


class Lookup:
    async def resolve_by_connection_id(self, *, connection_id: str, capability: str) -> ProviderConnection:
        return ProviderConnection(connection_id=connection_id, tenant_id="tenant-demo", capability=capability, base_url="https://provider.example.test", endpoint="/v1/commands", authentication=ProviderAuthentication(scheme="none"))


class Finalizer(OutboxFinalizer):
    def __init__(self) -> None: self.actions: list[str] = []
    async def accepted(self, **_kwargs): self.actions.append("accepted")
    async def rejected(self, **_kwargs): self.actions.append("rejected")
    async def terminal_failure(self, **_kwargs): self.actions.append("terminal")


class Transport:
    def __init__(self, result=None, error=None) -> None: self.result, self.error = result, error
    async def send_command(self, *, connection, command):
        if self.error:
            raise self.error
        return self.result or ProviderCommandResult(command_id=command.command_id, status="accepted", received_at=datetime.now(UTC))


async def test_empty_queue_and_accepted_command_have_structured_results() -> None:
    finalizer = Finalizer()
    worker = OutboxDispatchWorker(repository=Repository([]), connection_lookup=Lookup(), transport=Transport(), finalizer=finalizer, worker_id="worker-1")
    assert await worker.run_once() == WorkerRunResult()
    item = _claimed()
    result = await OutboxDispatchWorker(repository=Repository([item]), connection_lookup=Lookup(), transport=Transport(), finalizer=finalizer, worker_id="worker-1").run_once()
    assert (result.claimed, result.published, result.dead) == (1, 1, 0)
    assert finalizer.actions == ["accepted"]


async def test_retryable_failure_schedules_retry_without_terminal_finalization() -> None:
    repository = Repository([_claimed()])
    finalizer = Finalizer()
    result = await OutboxDispatchWorker(repository=repository, connection_lookup=Lookup(), transport=Transport(error=ProviderConnectionError("safe")), finalizer=finalizer, worker_id="worker-1", random_source=lambda: 0).run_once()
    assert result.retried == 1
    assert repository.retries == 1
    assert finalizer.actions == []
