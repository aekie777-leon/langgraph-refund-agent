"""Integration tests for PostgreSQL support-case persistence."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from agent.auth.config import ScimDirectoryConfig
from agent.auth.dependencies import require_access_scope
from agent.auth.directory import DirectoryInfrastructureUnavailableError
from agent.auth.scim_directory import ScimIdentityDirectory
from agent.cases.api import router as case_api_router
from agent.cases.api_errors import register_case_exception_handlers
from agent.cases.models import (
    CaseTrigger,
    HandoffPolicyInput,
    SupportCase,
    SupportCaseEvent,
)
from agent.cases.policy import determine_handoff_policy
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.cases.repository import (
    ActiveCaseConflictError,
    ConcurrentCaseUpdateError,
    DuplicateIdempotencyKeyError,
)
from agent.cases.runtime import get_case_service
from agent.cases.service import AssignmentTargetUnavailableError, CaseService
from agent.database import create_async_connection_pool
from agent.migrations import apply_migrations
from tests.fakes.identity import make_scope

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]
NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
SCOPE = make_scope("customer")
SUPERVISOR_SCOPE = make_scope("supervisor", user_id="sup-1")
_SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"


def _scim_config() -> ScimDirectoryConfig:
    return ScimDirectoryConfig.model_validate(
        {
            "base_url": "https://directory.example.test/scim/v2",
            "bearer_token": "integration-scim-secret",
            "user_id_attribute": "externalId",
            "tenant_id_attribute": "tenantId",
            "active_attribute": "active",
            "roles_attribute": "roles",
            "role_mapping": {
                "support_agent": ["Refund Agent"],
                "supervisor": ["Refund Supervisor"],
            },
        }
    )


def _scim_list_response(*, tenant_id: str, active: bool = True) -> dict:
    return {
        "schemas": [_SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "Resources": [
            {
                "schemas": [_SCIM_USER_SCHEMA],
                "externalId": "agent-7",
                "tenantId": tenant_id,
                "active": active,
                "roles": [{"value": "Refund Agent"}],
            }
        ],
    }


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context() -> AsyncIterator[tuple[AsyncConnectionPool, str]]:
    conninfo = os.getenv("CASE_TEST_POSTGRES_URI")
    if not conninfo:
        pytest.skip("CASE_TEST_POSTGRES_URI is not configured")

    apply_migrations(conninfo)
    pool = create_async_connection_pool(conninfo, min_size=1, max_size=4)
    await pool.open()
    await pool.wait(timeout=10)
    thread_prefix = f"case-integration-{uuid4()}"
    try:
        yield pool, thread_prefix
    finally:
        async with pool.connection() as connection:
            await connection.execute(
                """
                DELETE FROM case_management.support_case_events AS events
                USING case_management.support_cases AS cases
                WHERE events.case_id = cases.case_id
                  AND cases.thread_id LIKE %s
                """,
                (f"{thread_prefix}%",),
            )
            await connection.execute(
                """
                DELETE FROM case_management.support_cases
                WHERE thread_id LIKE %s
                """,
                (f"{thread_prefix}%",),
            )
        await pool.close()


def _trigger(thread_id: str, message_id: str) -> CaseTrigger:
    return CaseTrigger(
        thread_id=thread_id,
        source_message_id=message_id,
        order_id="ORD-10001",
        risk_level="medium",
        risk_categories=("self_harm",),
        triggering_message_excerpt="I need urgent help with my order.",
    )


def _decision():
    return determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="medium",
            semantic_risk_categories=("self_harm",),
        )
    )


@pytest.mark.anyio
async def test_create_round_trips_case_and_initial_event(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)

    result = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )

    assert result.case is not None
    stored = await repository.get_case(SCOPE, result.case.case_id)
    event = await repository.find_event_by_idempotency_key(
        SCOPE,
        f"message:{thread_id}:message-1",
    )
    assert stored == result.case
    assert event == result.event
    assert stored.risk_categories == ("self_harm",)
    assert stored.reason_codes == ("semantic_medium_self_harm",)
    assert stored.customer_id == "customer-a"
    assert stored.tenant_id == "tenant-demo"


@pytest.mark.anyio
async def test_duplicate_message_is_idempotent(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    trigger = _trigger(thread_id, "message-1")

    first = await service.record_handoff(SCOPE, trigger=trigger, decision=_decision())
    duplicate = await service.record_handoff(SCOPE, trigger=trigger, decision=_decision())

    assert duplicate.action == "duplicate_ignored"
    assert duplicate.case == first.case


@pytest.mark.anyio
async def test_same_thread_and_type_appends_persisted_event(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)

    first = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )
    second = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "message-2"),
        decision=_decision(),
    )

    assert first.case is not None
    assert second.case is not None
    assert second.action == "event_appended"
    assert second.case.case_id == first.case.case_id
    assert second.case.version == 2
    stored = await repository.get_case(SCOPE, first.case.case_id)
    assert stored == second.case


@pytest.mark.anyio
async def test_partial_unique_index_rejects_second_active_case(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    first = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )
    assert first.case is not None
    assert first.event is not None

    conflicting_case = SupportCase.model_validate(
        {
            **first.case.model_dump(mode="python"),
            "case_id": uuid4(),
            "source_message_id": "message-2",
        }
    )
    conflicting_event = SupportCaseEvent.model_validate(
        {
            **first.event.model_dump(mode="python"),
            "event_id": uuid4(),
            "idempotency_key": f"message:{thread_id}:message-2",
            "case_id": conflicting_case.case_id,
            "source_message_id": "message-2",
        }
    )

    with pytest.raises(ActiveCaseConflictError):
        await repository.create_case_with_event(
            SCOPE,
            case=conflicting_case,
            event=conflicting_event,
        )


@pytest.mark.anyio
async def test_stale_version_raises_concurrent_update_error(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )
    assert created.case is not None

    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE case_management.support_cases
            SET version = version + 1
            WHERE case_id = %s
            """,
            (created.case.case_id,),
        )

    stale_update = SupportCase.model_validate(
        {
            **created.case.model_dump(mode="python"),
            "status": "in_progress",
            "version": 2,
        }
    )
    event = SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=f"status:{created.case.case_id}:stale",
        case_id=created.case.case_id,
        event_type="status_changed",
        previous_priority=created.case.priority,
        current_priority=created.case.priority,
        previous_status="open",
        current_status="in_progress",
        actor="integration-test",
        customer_id=created.case.customer_id,
        tenant_id=created.case.tenant_id,
        created_at=NOW,
    )

    with pytest.raises(ConcurrentCaseUpdateError):
        await repository.update_case_with_event(
            SCOPE,
            case=stale_update,
            event=event,
            expected_version=1,
        )


@pytest.mark.anyio
async def test_duplicate_event_rolls_back_case_update(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )
    assert created.case is not None
    assert created.event is not None

    updated = SupportCase.model_validate(
        {
            **created.case.model_dump(mode="python"),
            "status": "in_progress",
            "version": 2,
        }
    )
    duplicate_event = SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=created.event.idempotency_key,
        case_id=created.case.case_id,
        event_type="status_changed",
        previous_priority=created.case.priority,
        current_priority=created.case.priority,
        previous_status="open",
        current_status="in_progress",
        actor="integration-test",
        customer_id=created.case.customer_id,
        tenant_id=created.case.tenant_id,
        created_at=NOW,
    )

    with pytest.raises(DuplicateIdempotencyKeyError):
        await repository.update_case_with_event(
            SCOPE,
            case=updated,
            event=duplicate_event,
            expected_version=1,
        )

    stored = await repository.get_case(SCOPE, created.case.case_id)
    assert stored is not None
    assert stored.status == "open"
    assert stored.version == 1


@pytest.mark.anyio
async def test_internal_api_round_trips_case_status_and_events(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "api-message-1"),
        decision=_decision(),
    )
    assert created.case is not None

    app = FastAPI()
    app.include_router(case_api_router)
    register_case_exception_handlers(app)
    app.dependency_overrides[get_case_service] = lambda: service
    app.dependency_overrides[require_access_scope] = lambda: SUPERVISOR_SCOPE

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        cases = await client.get(
            "/internal/support-cases",
            params={"thread_id": thread_id},
        )
        changed = await client.post(
            f"/internal/support-cases/{created.case.case_id}/status",
            json={
                "target_status": "in_progress",
                "request_id": "postgres-api-status-1",
            },
        )
        duplicate = await client.post(
            f"/internal/support-cases/{created.case.case_id}/status",
            json={
                "target_status": "in_progress",
                "request_id": "postgres-api-status-1",
            },
        )
        events = await client.get(
            f"/internal/support-cases/{created.case.case_id}/events"
        )

    assert cases.status_code == 200
    assert cases.json()["total"] == 1
    assert changed.status_code == 200
    assert changed.json()["action"] == "status_changed"
    assert duplicate.json()["action"] == "status_unchanged"
    assert events.status_code == 200
    assert events.json()["total"] == 2
    assert any(item["actor"] == SUPERVISOR_SCOPE.identity for item in events.json()["items"])


@pytest.mark.parametrize(
    ("tenant_id", "active"),
    [("tenant-other", True), ("tenant-demo", False)],
)
async def test_scim_rejected_assignment_does_not_write_postgres(
    postgres_context: tuple[AsyncConnectionPool, str],
    tenant_id: str,
    active: bool,
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    creator = CaseService(repository, clock=lambda: NOW)
    created = await creator.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "scim-rejected-message"),
        decision=_decision(),
    )
    assert created.case is not None

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_scim_list_response(tenant_id=tenant_id, active=active),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CaseService(
            repository,
            identity_directory=ScimIdentityDirectory(_scim_config(), client),
            clock=lambda: NOW,
        )
        with pytest.raises(AssignmentTargetUnavailableError):
            await service.assign_case(
                SUPERVISOR_SCOPE,
                case_id=created.case.case_id,
                agent_id="agent-7",
                request_id="scim-rejected-assignment",
            )

    stored = await repository.get_case(SUPERVISOR_SCOPE, created.case.case_id)
    events = await repository.list_case_events(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        limit=100,
        offset=0,
    )
    assert stored == created.case
    assert events.total == 1


async def test_scim_outage_does_not_write_postgres(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    creator = CaseService(repository, clock=lambda: NOW)
    created = await creator.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "scim-outage-message"),
        decision=_decision(),
    )
    assert created.case is not None

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream detail must remain private")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CaseService(
            repository,
            identity_directory=ScimIdentityDirectory(_scim_config(), client),
            clock=lambda: NOW,
        )
        with pytest.raises(DirectoryInfrastructureUnavailableError) as error:
            await service.assign_case(
                SUPERVISOR_SCOPE,
                case_id=created.case.case_id,
                agent_id="agent-7",
                request_id="scim-outage-assignment",
            )

    assert str(error.value) == "identity infrastructure is unavailable"
    stored = await repository.get_case(SUPERVISOR_SCOPE, created.case.case_id)
    events = await repository.list_case_events(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        limit=100,
        offset=0,
    )
    assert stored == created.case
    assert events.total == 1


async def test_scim_assignment_is_idempotent_under_postgres_concurrency(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    creator = CaseService(repository, clock=lambda: NOW)
    created = await creator.record_handoff(
        SCOPE,
        trigger=_trigger(thread_id, "scim-concurrent-message"),
        decision=_decision(),
    )
    assert created.case is not None

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_scim_list_response(tenant_id="tenant-demo"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CaseService(
            repository,
            identity_directory=ScimIdentityDirectory(_scim_config(), client),
            clock=lambda: NOW,
        )
        results = await asyncio.gather(
            *(
                service.assign_case(
                    SUPERVISOR_SCOPE,
                    case_id=created.case.case_id,
                    agent_id="agent-7",
                    request_id="scim-concurrent-assignment",
                )
                for _ in range(2)
            )
        )

    assert {result.action for result in results} == {"assigned", "status_unchanged"}
    stored = await repository.get_case(SUPERVISOR_SCOPE, created.case.case_id)
    events = await repository.list_case_events(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        limit=100,
        offset=0,
    )
    assert stored is not None
    assert stored.assigned_agent_id == "agent-7"
    assert events.total == 2
