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
from agent.cases.service import CaseService
from agent.database import create_async_connection_pool
from agent.migrations import apply_migrations

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]
NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


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
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )

    assert result.case is not None
    stored = await repository.get_case(result.case.case_id)
    event = await repository.find_event_by_idempotency_key(
        f"message:{thread_id}:message-1"
    )
    assert stored == result.case
    assert event == result.event
    assert stored.risk_categories == ("self_harm",)
    assert stored.reason_codes == ("semantic_medium_self_harm",)


@pytest.mark.anyio
async def test_duplicate_message_is_idempotent(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    trigger = _trigger(thread_id, "message-1")

    first = await service.record_handoff(trigger=trigger, decision=_decision())
    duplicate = await service.record_handoff(trigger=trigger, decision=_decision())

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
        trigger=_trigger(thread_id, "message-1"),
        decision=_decision(),
    )
    second = await service.record_handoff(
        trigger=_trigger(thread_id, "message-2"),
        decision=_decision(),
    )

    assert first.case is not None
    assert second.case is not None
    assert second.action == "event_appended"
    assert second.case.case_id == first.case.case_id
    assert second.case.version == 2
    stored = await repository.get_case(first.case.case_id)
    assert stored == second.case


@pytest.mark.anyio
async def test_partial_unique_index_rejects_second_active_case(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresCaseRepository(pool)
    service = CaseService(repository, clock=lambda: NOW)
    first = await service.record_handoff(
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
        created_at=NOW,
    )

    with pytest.raises(ConcurrentCaseUpdateError):
        await repository.update_case_with_event(
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
        created_at=NOW,
    )

    with pytest.raises(DuplicateIdempotencyKeyError):
        await repository.update_case_with_event(
            case=updated,
            event=duplicate_event,
            expected_version=1,
        )

    stored = await repository.get_case(created.case.case_id)
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
        trigger=_trigger(thread_id, "api-message-1"),
        decision=_decision(),
    )
    assert created.case is not None

    app = FastAPI()
    app.include_router(case_api_router)
    register_case_exception_handlers(app)
    app.dependency_overrides[get_case_service] = lambda: service

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
                "actor": "integration-agent",
            },
        )
        duplicate = await client.post(
            f"/internal/support-cases/{created.case.case_id}/status",
            json={
                "target_status": "in_progress",
                "request_id": "postgres-api-status-1",
                "actor": "integration-agent",
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
    assert any(item["actor"] == "integration-agent" for item in events.json()["items"])
