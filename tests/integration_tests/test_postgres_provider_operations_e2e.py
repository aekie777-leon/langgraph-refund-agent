"""Cross-component HTTP-to-worker E2E coverage for Provider operations."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent.auth.runtime import (
    configure_identity_runtime,
    create_identity_runtime,
    shutdown_identity_runtime,
)
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.integrations.finalization import PostgresOutboxFinalizer
from agent.integrations.inbox_postgres_finalizer import PostgresInboxFinalizer
from agent.integrations.inbox_worker import InboxProcessingWorker, InboxWorkerRunResult
from agent.integrations.models import (
    ProviderAuthentication,
    ProviderCommandResult,
    ProviderConnection,
)
from agent.integrations.outbox_worker import OutboxDispatchWorker, WorkerRunResult
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.provider_operations_api import router
from agent.integrations.provider_operations_api_errors import (
    register_provider_operations_exception_handlers,
)
from agent.integrations.provider_operations_postgres import (
    PostgresProviderOperationsRepository,
)
from agent.integrations.provider_operations_service import ProviderOperationsService
from agent.integrations.repository import LeaseConflictError
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from tests.integration_tests.test_postgres_provider_inbox import (
    _claimed_inbox,
    _events_for_idempotency_key,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    _case_created_event,
    _delivery_case,
    _delivery_envelope,
    _envelope,
    _operation,
    _queued_event,
    _scope,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    postgres_context as _postgres_context,
)
from tests.integration_tests.test_postgres_provider_operations import (
    _make_dead_outbox,
    _manual_review_operation_command,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]

_SUPERVISOR_TOKEN = "e2e-supervisor-token"
_OTHER_SUPERVISOR_TOKEN = "e2e-other-supervisor-token"
_AGENT_TOKEN = "e2e-support-agent-token"
_REDRIVE_BODY = {
    "request_id": "ops:e2e-redrive-1",
    "reason_code": "transient_incident_resolved",
}
_FUTURE_TIME = datetime(2099, 8, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context() -> AsyncIterator[tuple[AsyncConnectionPool, str]]:
    """Reuse the disposable messaging schema and tenant-scoped cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


@pytest.fixture(autouse=True)
async def clean_identity_runtime() -> AsyncIterator[None]:
    """Keep each shared-runtime E2E isolated from every neighboring test."""
    await shutdown_identity_runtime()
    try:
        yield
    finally:
        await shutdown_identity_runtime()


def _configure_demo_identities(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: str,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IDENTITY_PROVIDER", "demo")
    monkeypatch.setenv("IDENTITY_DIRECTORY", "none")
    monkeypatch.setenv(
        "DEMO_IDENTITY_TOKENS",
        json.dumps(
            {
                _SUPERVISOR_TOKEN: {
                    "user_id": "provider-ops-supervisor",
                    "tenant_id": tenant_id,
                    "role": "supervisor",
                },
                _OTHER_SUPERVISOR_TOKEN: {
                    "user_id": "other-provider-ops-supervisor",
                    "tenant_id": f"{tenant_id}-other",
                    "role": "supervisor",
                },
                _AGENT_TOKEN: {
                    "user_id": "support-agent",
                    "tenant_id": tenant_id,
                    "role": "support_agent",
                },
            }
        ),
    )
    configure_identity_runtime(
        create_identity_runtime(
            os.environ,
            studio_auth_disabled=True,
        )
    )


def _app(pool: AsyncConnectionPool) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    register_provider_operations_exception_handlers(app)
    app.state.provider_operations_service = ProviderOperationsService(
        PostgresProviderOperationsRepository(pool)
    )
    return app


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _ConnectionLookup:
    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def resolve_by_connection_id(
        self,
        *,
        connection_id: str,
        capability: str,
    ) -> ProviderConnection:
        return ProviderConnection(
            connection_id=connection_id,
            tenant_id=self._tenant_id,
            capability=capability,
            base_url="https://provider.invalid",
            endpoint="/commands",
            authentication=ProviderAuthentication(scheme="none"),
        )


class _AcceptingTransport:
    def __init__(self) -> None:
        self.command_ids: list[UUID] = []

    async def send_command(self, *, connection, command) -> ProviderCommandResult:
        self.command_ids.append(command.command_id)
        return ProviderCommandResult(
            command_id=command.command_id,
            status="accepted",
            provider_operation_id="e2e-provider-operation-sensitive",
            provider_reference="e2e-provider-reference-sensitive",
            received_at=datetime.now(UTC),
        )


class _RejectingTransport:
    async def send_command(self, *, connection, command) -> ProviderCommandResult:
        return ProviderCommandResult(
            command_id=command.command_id,
            status="rejected",
            provider_operation_id="e2e-rejected-operation-sensitive",
            provider_reference="e2e-rejected-reference-sensitive",
            received_at=datetime.now(UTC),
        )


class _TechnicalFailureTransport:
    async def send_command(self, *, connection, command) -> ProviderCommandResult:
        raise ValueError("sensitive fake transport validation detail")


async def _redrive_counts(
    pool: AsyncConnectionPool, *, tenant_id: str, target_id: UUID
):
    async with pool.connection() as connection:
        outbox = (
            await (
                await connection.execute(
                    """
                    SELECT count(*) FROM integration.outbox_redrives
                    WHERE tenant_id = %s AND command_id = %s
                    """,
                    (tenant_id, target_id),
                )
            ).fetchone()
        )[0]
        inbox = (
            await (
                await connection.execute(
                    """
                    SELECT count(*) FROM integration.inbox_redrives
                    WHERE tenant_id = %s AND inbox_id = %s
                    """,
                    (tenant_id, target_id),
                )
            ).fetchone()
        )[0]
    return outbox, inbox


async def test_supervisor_redrives_operation_then_outbox_worker_accepts_once(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    case, operation, command = await _manual_review_operation_command(pool, tenant_id)
    app = _app(pool)
    path = f"/internal/provider-operations/outbox/{command.command_id}/redrives"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        recovered = await client.post(
            path,
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=_REDRIVE_BODY,
        )
        pre_worker_replay = await client.post(
            path,
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=_REDRIVE_BODY,
        )

    assert recovered.status_code == pre_worker_replay.status_code == 200
    assert recovered.json() == pre_worker_replay.json()
    assert set(recovered.json()) == {
        "request_id",
        "reason_code",
        "actor",
        "previous_cycle",
        "new_cycle",
        "created_at",
    }
    response_text = recovered.text
    for forbidden in (
        "payload",
        "provider_connection",
        "provider_operation",
        "provider_reference",
        "customer_id",
        "source_message_id",
    ):
        assert forbidden not in response_text

    integration = PostgresIntegrationRepository(pool)
    redriven = await integration.get_outbox_message(command.command_id)
    assert redriven is not None
    assert redriven.status == "retry_scheduled"
    assert redriven.delivery_cycle == 2
    assert redriven.attempts_in_cycle == 0
    transport = _AcceptingTransport()
    worker = OutboxDispatchWorker(
        repository=integration,
        connection_lookup=_ConnectionLookup(tenant_id),
        transport=transport,
        finalizer=PostgresOutboxFinalizer(pool),
        worker_id="provider-ops-e2e-outbox-worker",
        batch_size=1,
        lease_seconds=60,
    )

    assert await worker.run_once() == WorkerRunResult(claimed=1, published=1)
    assert transport.command_ids == [command.command_id]
    finalized = await integration.get_outbox_message(command.command_id)
    assert finalized is not None
    assert finalized.status == "published"
    assert finalized.delivery_cycle == 2
    assert finalized.attempts_in_cycle == 1

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT status, requires_manual_review, support_case_id, version
                FROM case_management.order_operations
                WHERE tenant_id = %s AND operation_id = %s
                """,
                (tenant_id, operation.operation_id),
            )
            operation_row = await cursor.fetchone()
            await cursor.execute(
                """
                SELECT status, priority, assigned_agent_id, version
                FROM case_management.support_cases
                WHERE tenant_id = %s AND case_id = %s
                """,
                (tenant_id, case.case_id),
            )
            case_row = await cursor.fetchone()
            await cursor.execute(
                """
                SELECT count(*) AS count
                FROM case_management.support_case_events
                WHERE tenant_id = %s AND case_id = %s
                  AND event_type = 'provider_redrive'
                """,
                (tenant_id, case.case_id),
            )
            case_audits = (await cursor.fetchone())["count"]
            await cursor.execute(
                """
                SELECT count(*) AS count
                FROM integration.outbox_delivery_attempts
                WHERE command_id = %s AND delivery_cycle = 2
                """,
                (command.command_id,),
            )
            cycle_attempts = (await cursor.fetchone())["count"]

    assert operation_row == {
        "status": "submitted",
        "requires_manual_review": False,
        "support_case_id": None,
        "version": operation.version + 2,
    }
    assert case_row == {
        "status": case.status,
        "priority": case.priority,
        "assigned_agent_id": case.assigned_agent_id,
        "version": case.version,
    }
    assert case_audits == cycle_attempts == 1
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=command.command_id
    ) == (1, 0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        post_worker_replay = await client.post(
            path,
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=_REDRIVE_BODY,
        )

    assert post_worker_replay.status_code == 200
    assert post_worker_replay.json() == recovered.json()
    assert await worker.run_once() == WorkerRunResult()
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=command.command_id
    ) == (1, 0)


async def test_supervisor_redrives_inbox_then_worker_replays_canonical_path_once(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    (
        scope,
        operation_repository,
        integration,
        queued_operation,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    assert claimed.lease_id is not None and claimed.lease_owner is not None
    await integration.mark_inbox_failed(
        inbox_id=inbox.inbox_id,
        lease_id=claimed.lease_id,
        lease_owner=claimed.lease_owner,
        error_code="canonical_processing_failed",
        error_message="Sensitive diagnostic must not leave persistence.",
    )
    callback_columns = """
        provider_connection_id, event_id, tenant_id, schema_version, event_type,
        command_id, aggregate_type, aggregate_id, payload, raw_body_sha256
    """
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {callback_columns} FROM integration.inbox_messages "
                "WHERE tenant_id = %s AND inbox_id = %s",
                (tenant_id, inbox.inbox_id),
            )
            original_callback = await cursor.fetchone()
    operation_before = await operation_repository.get_operation(
        scope, queued_operation.operation_id
    )
    assert operation_before is not None and operation_before.status == "submitted"
    app = _app(pool)
    path = f"/internal/provider-operations/inbox/{inbox.inbox_id}/redrives"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        recovered = await client.post(
            path,
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=_REDRIVE_BODY,
        )

    assert recovered.status_code == 200
    redriven = await integration.get_inbox_message(inbox.inbox_id)
    assert redriven is not None
    assert redriven.status == "received"
    assert redriven.processing_cycle == 2
    assert redriven.attempts_in_cycle == 0
    worker = InboxProcessingWorker(
        repository=integration,
        finalizer=PostgresInboxFinalizer(pool),
        worker_id="provider-ops-e2e-inbox-worker",
        batch_size=1,
        lease_seconds=60,
    )

    assert await worker.run_once() == InboxWorkerRunResult(claimed=1, applied=1)
    processed = await integration.get_inbox_message(inbox.inbox_id)
    assert processed is not None
    assert processed.status == "processed"
    assert processed.processing_cycle == 2
    assert processed.attempts_in_cycle == 1
    assert processed.processing_attempts == 2
    operation_after = await operation_repository.get_operation(
        scope, queued_operation.operation_id
    )
    assert operation_after is not None
    assert operation_after.status == "processing"
    assert operation_after.version == operation_before.version + 1
    event_key = f"provider-webhook:{inbox.inbox_id}:operation-status"
    domain_events = await _events_for_idempotency_key(pool, event_key)
    assert len(domain_events) == 1

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {callback_columns} FROM integration.inbox_messages "
                "WHERE tenant_id = %s AND inbox_id = %s",
                (tenant_id, inbox.inbox_id),
            )
            preserved_callback = await cursor.fetchone()
    assert preserved_callback == original_callback
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=inbox.inbox_id
    ) == (0, 1)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        replay = await client.post(
            path,
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=_REDRIVE_BODY,
        )

    assert replay.status_code == 200
    assert replay.json() == recovered.json()
    assert await worker.run_once() == InboxWorkerRunResult()
    replayed = await integration.get_inbox_message(inbox.inbox_id)
    assert replayed is not None
    assert replayed.processing_cycle == 2
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=inbox.inbox_id
    ) == (0, 1)
    assert await _events_for_idempotency_key(pool, event_key) == domain_events


async def test_provider_business_rejection_redrive_is_safe_and_non_mutating(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80801")
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id),
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    await _make_dead_outbox(
        pool,
        command.command_id,
        failure_kind="provider_rejection",
        outcome="provider_rejected",
    )
    app = _app(pool)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/internal/provider-operations/outbox/{command.command_id}/redrives",
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=_REDRIVE_BODY,
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "provider_rejection",
            "message": "Provider business rejections cannot be redriven.",
        }
    }
    assert "provider_rejected" not in response.text
    stored = await PostgresIntegrationRepository(pool).get_outbox_message(
        command.command_id
    )
    assert stored is not None
    assert stored.status == "dead"
    assert stored.delivery_cycle == 1
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=command.command_id
    ) == (0, 0)


async def test_http_auth_and_cross_tenant_non_disclosure_use_shared_boundaries(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80802")
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id),
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    await _make_dead_outbox(pool, command.command_id)
    app = _app(pool)
    path = f"/internal/provider-operations/outbox/{command.command_id}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthenticated = await client.get(path)
        forbidden = await client.get(
            "/internal/provider-operations/queues",
            headers=_authorization(_AGENT_TOKEN),
        )
        cross_tenant = await client.get(
            path,
            headers=_authorization(_OTHER_SUPERVISOR_TOKEN),
        )
        absent = await client.get(
            f"/internal/provider-operations/outbox/{uuid4()}",
            headers=_authorization(_SUPERVISOR_TOKEN),
        )

    assert unauthenticated.status_code == 401
    assert forbidden.status_code == 403
    assert cross_tenant.status_code == absent.status_code == 404
    assert (
        cross_tenant.json()
        == absent.json()
        == {
            "error": {
                "code": "provider_operations_not_found",
                "message": "The requested Provider operations resource does not exist.",
            }
        }
    )


async def test_real_operation_failure_redrive_and_cycle_two_acceptance(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real cycle-one terminal failure must not collide with cycle two."""
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    scope = _scope(tenant_id)
    operation_repository = PostgresOrderOperationRepository(pool)
    created = _operation(tenant_id=tenant_id, source_message_id=f"source-{uuid4()}")
    await operation_repository.create_operation_with_events(
        scope, operation=created, events=()
    )
    queued = created.model_copy(update={"status": "queued", "version": 2})
    command = _envelope(queued)
    await operation_repository.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    integration = PostgresIntegrationRepository(pool)
    cycle_one_worker = OutboxDispatchWorker(
        repository=integration,
        connection_lookup=_ConnectionLookup(tenant_id),
        transport=_TechnicalFailureTransport(),
        finalizer=PostgresOutboxFinalizer(pool),
        worker_id="cycle-one-terminal-worker",
        batch_size=1,
        lease_seconds=60,
    )
    assert await cycle_one_worker.run_once() == WorkerRunResult(claimed=1, dead=1)
    failed = await integration.get_outbox_message(command.command_id)
    assert failed is not None and failed.status == "dead"
    operation_after_failure = await operation_repository.get_operation(
        scope, queued.operation_id
    )
    assert operation_after_failure is not None
    assert operation_after_failure.status == "manual_review"
    assert operation_after_failure.support_case_id is not None

    app = _app(pool)
    path = f"/internal/provider-operations/outbox/{command.command_id}/redrives"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        redrive = await client.post(
            path,
            headers=_authorization(_SUPERVISOR_TOKEN),
            json={**_REDRIVE_BODY, "request_id": "ops:real-operation-cycle-2"},
        )
    assert redrive.status_code == 200

    cycle_two_worker = OutboxDispatchWorker(
        repository=integration,
        connection_lookup=_ConnectionLookup(tenant_id),
        transport=_AcceptingTransport(),
        finalizer=PostgresOutboxFinalizer(pool),
        worker_id="cycle-two-accepting-worker",
        batch_size=1,
        lease_seconds=60,
    )
    assert await cycle_two_worker.run_once() == WorkerRunResult(claimed=1, published=1)
    finalized = await integration.get_outbox_message(command.command_id)
    assert finalized is not None
    assert (finalized.status, finalized.delivery_cycle) == ("published", 2)
    operation_after_acceptance = await operation_repository.get_operation(
        scope, queued.operation_id
    )
    assert operation_after_acceptance is not None
    assert operation_after_acceptance.status == "submitted"

    async with pool.connection() as connection:
        rows = await (
            await connection.execute(
                """
                SELECT idempotency_key, previous_status, current_status
                FROM case_management.order_operation_events
                WHERE operation_id = %s
                  AND idempotency_key LIKE %s
                ORDER BY created_at, event_id
                """,
                (queued.operation_id, "provider-command:%:status"),
            )
        ).fetchall()
        attempts = await (
            await connection.execute(
                """
                SELECT delivery_cycle, attempt_number, outcome
                FROM integration.outbox_delivery_attempts
                WHERE command_id = %s ORDER BY delivery_cycle, attempt_number
                """,
                (command.command_id,),
            )
        ).fetchall()
    assert rows == [
        (
            f"provider-command:{command.command_id}:cycle:1:status",
            "queued",
            "manual_review",
        ),
        (
            f"provider-command:{command.command_id}:cycle:2:status",
            "queued",
            "submitted",
        ),
    ]
    assert attempts == [(1, 1, "terminal_failure"), (2, 1, "accepted")]


async def test_real_support_case_failure_redrive_and_cycle_two_rejection(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated rejected case events remain immutable across delivery cycles."""
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80803")
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id),
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    integration = PostgresIntegrationRepository(pool)
    first_worker = OutboxDispatchWorker(
        repository=integration,
        connection_lookup=_ConnectionLookup(tenant_id),
        transport=_TechnicalFailureTransport(),
        finalizer=PostgresOutboxFinalizer(pool),
        worker_id="case-cycle-one-terminal-worker",
        batch_size=1,
        lease_seconds=60,
    )
    assert await first_worker.run_once() == WorkerRunResult(claimed=1, dead=1)
    app = _app(pool)
    body = {**_REDRIVE_BODY, "request_id": "ops:real-case-cycle-2"}
    path = f"/internal/provider-operations/outbox/{command.command_id}/redrives"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        redrive = await client.post(
            path, headers=_authorization(_SUPERVISOR_TOKEN), json=body
        )
    assert redrive.status_code == 200
    second_worker = OutboxDispatchWorker(
        repository=integration,
        connection_lookup=_ConnectionLookup(tenant_id),
        transport=_RejectingTransport(),
        finalizer=PostgresOutboxFinalizer(pool),
        worker_id="case-cycle-two-rejecting-worker",
        batch_size=1,
        lease_seconds=60,
    )
    assert await second_worker.run_once() == WorkerRunResult(claimed=1, dead=1)

    async with pool.connection() as connection:
        events = await (
            await connection.execute(
                """
                SELECT idempotency_key, provider_command_status
                FROM case_management.support_case_events
                WHERE case_id = %s AND event_type = 'provider_update'
                ORDER BY created_at, event_id
                """,
                (case.case_id,),
            )
        ).fetchall()
        attempts = await (
            await connection.execute(
                """
                SELECT delivery_cycle, outcome
                FROM integration.outbox_delivery_attempts
                WHERE command_id = %s ORDER BY delivery_cycle
                """,
                (command.command_id,),
            )
        ).fetchall()
    assert events == [
        (f"provider-command:{command.command_id}:cycle:1:rejected", "rejected"),
        (f"provider-command:{command.command_id}:cycle:2:rejected", "rejected"),
    ]
    assert attempts == [(1, "terminal_failure"), (2, "provider_rejected")]


async def test_cross_tenant_posts_are_identical_and_non_mutating_for_both_queues(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    (
        _,
        _,
        integration,
        _,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    assert claimed.lease_id is not None and claimed.lease_owner is not None
    await integration.mark_inbox_failed(
        inbox_id=inbox.inbox_id,
        lease_id=claimed.lease_id,
        lease_owner=claimed.lease_owner,
        error_code="cross_tenant_fixture_failed",
        error_message="Safe fixture failure.",
    )
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80804")
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id),
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    await _make_dead_outbox(pool, command.command_id)
    app = _app(pool)
    safe_not_found = {
        "error": {
            "code": "provider_operations_not_found",
            "message": "The requested Provider operations resource does not exist.",
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        cross_outbox = await client.post(
            f"/internal/provider-operations/outbox/{command.command_id}/redrives",
            headers=_authorization(_OTHER_SUPERVISOR_TOKEN),
            json={**_REDRIVE_BODY, "request_id": "ops:cross-outbox"},
        )
        missing_outbox = await client.post(
            f"/internal/provider-operations/outbox/{uuid4()}/redrives",
            headers=_authorization(_SUPERVISOR_TOKEN),
            json={**_REDRIVE_BODY, "request_id": "ops:missing-outbox"},
        )
        cross_inbox = await client.post(
            f"/internal/provider-operations/inbox/{inbox.inbox_id}/redrives",
            headers=_authorization(_OTHER_SUPERVISOR_TOKEN),
            json={**_REDRIVE_BODY, "request_id": "ops:cross-inbox"},
        )
        missing_inbox = await client.post(
            f"/internal/provider-operations/inbox/{uuid4()}/redrives",
            headers=_authorization(_SUPERVISOR_TOKEN),
            json={**_REDRIVE_BODY, "request_id": "ops:missing-inbox"},
        )
    assert [
        response.status_code
        for response in (cross_outbox, missing_outbox, cross_inbox, missing_inbox)
    ] == [404, 404, 404, 404]
    assert all(
        response.json() == safe_not_found
        for response in (cross_outbox, missing_outbox, cross_inbox, missing_inbox)
    )
    outbox_after = await integration.get_outbox_message(command.command_id)
    inbox_after = await integration.get_inbox_message(inbox.inbox_id)
    assert outbox_after is not None and outbox_after.status == "dead"
    assert inbox_after is not None and inbox_after.status == "failed"
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=command.command_id
    ) == (0, 0)
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=inbox.inbox_id
    ) == (0, 0)


async def test_inbox_concurrent_replay_and_cross_queue_request_namespace(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    (_, _, integration, _, inbox, claimed) = await _claimed_inbox(
        pool, tenant_id, "processing"
    )
    assert claimed.lease_id is not None and claimed.lease_owner is not None
    await integration.mark_inbox_failed(
        inbox_id=inbox.inbox_id,
        lease_id=claimed.lease_id,
        lease_owner=claimed.lease_owner,
        error_code="concurrent_fixture_failed",
        error_message="Safe fixture failure.",
    )
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80805")
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id),
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    await _make_dead_outbox(pool, command.command_id)
    app = _app(pool)
    body = {**_REDRIVE_BODY, "request_id": "ops:shared-queue-namespace"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        inbox_path = f"/internal/provider-operations/inbox/{inbox.inbox_id}/redrives"
        first, second = await asyncio.wait_for(
            asyncio.gather(
                client.post(
                    inbox_path,
                    headers=_authorization(_SUPERVISOR_TOKEN),
                    json=body,
                ),
                client.post(
                    inbox_path,
                    headers=_authorization(_SUPERVISOR_TOKEN),
                    json=body,
                ),
            ),
            timeout=10,
        )
        outbox_response = await client.post(
            f"/internal/provider-operations/outbox/{command.command_id}/redrives",
            headers=_authorization(_SUPERVISOR_TOKEN),
            json=body,
        )
    assert first.status_code == second.status_code == outbox_response.status_code == 200
    assert first.json() == second.json()
    assert first.json()["new_cycle"] == outbox_response.json()["new_cycle"] == 2
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=inbox.inbox_id
    ) == (0, 1)
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=command.command_id
    ) == (1, 0)


async def test_malformed_json_is_safe_422_and_valid_missing_auth_is_401(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    app = _app(pool)
    target = uuid4()
    path = f"/internal/provider-operations/outbox/{target}/redrives"
    secret_marker = "sensitive_payload_marker"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        malformed = await client.post(
            path,
            headers={
                **_authorization(_SUPERVISOR_TOKEN),
                "Content-Type": "application/json",
            },
            content=f'{{"request_id":"{secret_marker}",'.encode(),
        )
        unauthenticated = await client.post(path, json=_REDRIVE_BODY)
    assert malformed.status_code == 422
    assert malformed.json() == {
        "error": {
            "code": "provider_operations_request_invalid",
            "message": "The Provider operations request is invalid.",
        }
    }
    assert secret_marker not in malformed.text
    assert unauthenticated.status_code == 401


async def test_eighth_lease_expiry_creates_review_state_then_http_redrives(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    scope = _scope(tenant_id)
    operation_repository = PostgresOrderOperationRepository(pool)
    created = _operation(tenant_id=tenant_id, source_message_id=f"source-{uuid4()}")
    await operation_repository.create_operation_with_events(
        scope, operation=created, events=()
    )
    queued = created.model_copy(update={"status": "queued", "version": 2})
    command = _envelope(queued)
    await operation_repository.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    integration = PostgresIntegrationRepository(pool)
    for attempt_number in range(1, 9):
        claimed = await integration.claim_due_outbox(
            worker_id=f"expiry-worker-{attempt_number}",
            batch_size=1,
            lease_seconds=60,
        )
        assert len(claimed) == 1
        async with pool.connection() as connection:
            await connection.execute(
                """
                UPDATE integration.outbox_messages
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE command_id = %s AND lease_id = %s
                """,
                (command.command_id, claimed[0].lease_id),
            )
        assert await integration.recover_expired_outbox_leases(batch_size=1) == 1

    exhausted = await integration.get_outbox_message(command.command_id)
    assert exhausted is not None
    assert (exhausted.status, exhausted.attempts_in_cycle) == ("dead", 8)
    reviewed = await operation_repository.get_operation(scope, queued.operation_id)
    assert reviewed is not None
    assert reviewed.status == "manual_review"
    assert reviewed.requires_manual_review is True
    assert reviewed.support_case_id is not None

    app = _app(pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/internal/provider-operations/outbox/{command.command_id}/redrives",
            headers=_authorization(_SUPERVISOR_TOKEN),
            json={**_REDRIVE_BODY, "request_id": "ops:eighth-expiry-recovery"},
        )
    assert response.status_code == 200
    recovered = await integration.get_outbox_message(command.command_id)
    assert recovered is not None
    assert (
        recovered.status,
        recovered.delivery_cycle,
        recovered.attempts_in_cycle,
    ) == (
        "retry_scheduled",
        2,
        0,
    )
    queued_again = await operation_repository.get_operation(scope, queued.operation_id)
    assert queued_again is not None
    assert queued_again.status == "queued"
    assert queued_again.support_case_id is None
    async with pool.connection() as connection:
        attempts = await (
            await connection.execute(
                """
                SELECT attempt_number, outcome, safe_error_code,
                       finished_at >= started_at
                FROM integration.outbox_delivery_attempts
                WHERE command_id = %s ORDER BY attempt_number
                """,
                (command.command_id,),
            )
        ).fetchall()
    assert len(attempts) == 8
    assert all(row[1] == "lease_expired" and row[3] for row in attempts)
    assert attempts[-1][2] == "lease_expired_attempts_exhausted"


async def test_future_inbox_processed_finalization_is_monotonic(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    (_, _, integration, _, inbox, claimed) = await _claimed_inbox(
        pool, tenant_id, "processing"
    )
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE integration.inbox_messages
                SET received_at = %s, updated_at = %s
                WHERE inbox_id = %s
                """,
                (_FUTURE_TIME, _FUTURE_TIME, inbox.inbox_id),
            )
            await connection.execute(
                """
                UPDATE integration.inbox_processing_attempts
                SET started_at = %s WHERE attempt_id = %s
                """,
                (_FUTURE_TIME, claimed.attempt.attempt_id),
            )
    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed,
        retry_available_at=datetime(2100, 1, 1, tzinfo=UTC),
    )
    assert result.action == "applied"
    processed = await integration.get_inbox_message(inbox.inbox_id)
    assert processed is not None
    assert processed.status == "processed"
    assert processed.updated_at >= _FUTURE_TIME
    assert processed.processed_at is not None
    assert processed.processed_at >= _FUTURE_TIME
    async with pool.connection() as connection:
        attempt = await (
            await connection.execute(
                """
                SELECT finished_at, started_at
                FROM integration.inbox_processing_attempts
                WHERE attempt_id = %s
                """,
                (claimed.attempt.attempt_id,),
            )
        ).fetchone()
    assert attempt[0] >= attempt[1] >= _FUTURE_TIME


async def test_future_inbox_failed_finalization_is_monotonic(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    (_, _, integration, _, inbox, claimed) = await _claimed_inbox(
        pool, tenant_id, "processing"
    )
    assert claimed.lease_id is not None and claimed.lease_owner is not None
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE integration.inbox_messages
                SET received_at = %s, updated_at = %s
                WHERE inbox_id = %s
                """,
                (_FUTURE_TIME, _FUTURE_TIME, inbox.inbox_id),
            )
            await connection.execute(
                """
                UPDATE integration.inbox_processing_attempts
                SET started_at = %s WHERE attempt_id = %s
                """,
                (_FUTURE_TIME, claimed.attempt.attempt_id),
            )
    await integration.mark_inbox_failed(
        inbox_id=inbox.inbox_id,
        lease_id=claimed.lease_id,
        lease_owner=claimed.lease_owner,
        error_code="future_failure",
        error_message="Safe future failure.",
    )
    failed = await integration.get_inbox_message(inbox.inbox_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.updated_at >= _FUTURE_TIME
    assert failed.failed_at is not None
    assert failed.failed_at >= _FUTURE_TIME
    async with pool.connection() as connection:
        attempt = await (
            await connection.execute(
                """
                SELECT finished_at, started_at
                FROM integration.inbox_processing_attempts
                WHERE attempt_id = %s
                """,
                (claimed.attempt.attempt_id,),
            )
        ).fetchone()
    assert attempt[0] >= attempt[1] >= _FUTURE_TIME


async def test_redrive_and_stale_finalizer_converge_without_deadlock(
    postgres_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, tenant_id = postgres_context
    _configure_demo_identities(monkeypatch, tenant_id)
    scope = _scope(tenant_id)
    operation_repository = PostgresOrderOperationRepository(pool)
    created = _operation(tenant_id=tenant_id, source_message_id=f"source-{uuid4()}")
    await operation_repository.create_operation_with_events(
        scope, operation=created, events=()
    )
    queued = created.model_copy(update={"status": "queued", "version": 2})
    command = _envelope(queued)
    await operation_repository.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    integration = PostgresIntegrationRepository(pool)
    stale_claim = (
        await integration.claim_due_outbox(
            worker_id="stale-finalizer-worker", batch_size=1, lease_seconds=60
        )
    )[0]
    finalizer = PostgresOutboxFinalizer(pool)
    await finalizer.terminal_failure(
        claimed=stale_claim,
        failure_kind="validation_error",
        error_code="cycle_one_failed",
        error_message="Safe cycle-one failure.",
    )
    app = _app(pool)
    path = f"/internal/provider-operations/outbox/{command.command_id}/redrives"
    stale_acceptance = ProviderCommandResult(
        command_id=command.command_id,
        status="accepted",
        provider_reference="stale-reference-sensitive",
        received_at=datetime.now(UTC),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        redrive_result, stale_result = await asyncio.wait_for(
            asyncio.gather(
                client.post(
                    path,
                    headers=_authorization(_SUPERVISOR_TOKEN),
                    json={
                        **_REDRIVE_BODY,
                        "request_id": "ops:stale-finalizer-race",
                    },
                ),
                finalizer.accepted(
                    claimed=stale_claim,
                    result=stale_acceptance,
                ),
                return_exceptions=True,
            ),
            timeout=10,
        )
    assert redrive_result.status_code == 200
    assert isinstance(stale_result, LeaseConflictError)
    converged = await integration.get_outbox_message(command.command_id)
    assert converged is not None
    assert (converged.status, converged.delivery_cycle) == ("retry_scheduled", 2)
    operation = await operation_repository.get_operation(scope, queued.operation_id)
    assert operation is not None and operation.status == "queued"
    assert await _redrive_counts(
        pool, tenant_id=tenant_id, target_id=command.command_id
    ) == (1, 0)
