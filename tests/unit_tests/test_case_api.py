"""Unit tests for the internal support-case HTTP API."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from agent.cases.api import router
from agent.cases.api_errors import register_case_exception_handlers
from agent.cases.models import CaseTrigger, HandoffPolicyInput
from agent.cases.policy import determine_handoff_policy
from agent.cases.repository import CasePersistenceError
from agent.cases.runtime import get_case_service
from agent.cases.service import CaseService
from tests.support_cases import InMemoryCaseRepository

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app_with_service(service: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    register_case_exception_handlers(app)
    app.dependency_overrides[get_case_service] = lambda: service
    return app


async def _create_case(service: CaseService):
    return await service.record_handoff(
        trigger=CaseTrigger(
            thread_id="api-thread-1",
            source_message_id="api-message-1",
            order_id="ORD-10001",
            risk_level="medium",
            risk_categories=("self_harm",),
            triggering_message_excerpt="Please help with my order.",
        ),
        decision=determine_handoff_policy(
            HandoffPolicyInput(
                semantic_risk_level="medium",
                semantic_risk_categories=("self_harm",),
            )
        ),
    )


async def test_openapi_contains_all_internal_case_routes() -> None:
    app = _app_with_service(object())
    paths = app.openapi()["paths"]

    assert "/internal/support-cases" in paths
    assert "/internal/support-cases/{case_id}" in paths
    assert "/internal/support-cases/{case_id}/events" in paths
    assert "/internal/support-cases/{case_id}/status" in paths


async def test_cases_can_be_filtered_and_read_with_events() -> None:
    repository = InMemoryCaseRepository()
    service = CaseService(repository, clock=lambda: NOW)
    created = await _create_case(service)
    assert created.case is not None
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        cases = await client.get(
            "/internal/support-cases",
            params={
                "status": "open",
                "priority": "p2",
                "case_type": "safety_review",
                "thread_id": "api-thread-1",
                "order_id": "ORD-10001",
            },
        )
        case = await client.get(f"/internal/support-cases/{created.case.case_id}")
        events = await client.get(
            f"/internal/support-cases/{created.case.case_id}/events"
        )

    assert cases.status_code == 200
    assert cases.json()["total"] == 1
    assert cases.json()["items"][0]["case_id"] == str(created.case.case_id)
    assert case.status_code == 200
    assert case.json()["thread_id"] == "api-thread-1"
    assert events.status_code == 200
    assert events.json()["total"] == 1
    assert events.json()["items"][0]["event_type"] == "case_created"


async def test_status_update_is_idempotent_and_audited() -> None:
    repository = InMemoryCaseRepository()
    service = CaseService(repository, clock=lambda: NOW)
    created = await _create_case(service)
    assert created.case is not None
    app = _app_with_service(service)
    path = f"/internal/support-cases/{created.case.case_id}/status"
    payload = {
        "target_status": "in_progress",
        "request_id": "api-status-1",
        "actor": "agent-7",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        changed = await client.post(path, json=payload)
        duplicate = await client.post(path, json=payload)
        events = await client.get(
            f"/internal/support-cases/{created.case.case_id}/events"
        )

    assert changed.status_code == 200
    assert changed.json()["action"] == "status_changed"
    assert changed.json()["case"]["status"] == "in_progress"
    assert duplicate.status_code == 200
    assert duplicate.json()["action"] == "status_unchanged"
    assert events.json()["total"] == 2
    assert any(item["actor"] == "agent-7" for item in events.json()["items"])


async def test_on_hold_requires_a_reason_at_the_api_boundary() -> None:
    app = _app_with_service(object())
    case_id = UUID("00000000-0000-0000-0000-000000000001")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/internal/support-cases/{case_id}/status",
            json={
                "target_status": "on_hold",
                "request_id": "api-status-2",
                "actor": "agent-7",
            },
        )

    assert response.status_code == 422


async def test_invalid_transition_returns_a_conflict() -> None:
    repository = InMemoryCaseRepository()
    service = CaseService(repository, clock=lambda: NOW)
    created = await _create_case(service)
    assert created.case is not None
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/internal/support-cases/{created.case.case_id}/status",
            json={
                "target_status": "resolved",
                "request_id": "api-status-3",
                "actor": "agent-7",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ("invalid_case_status_transition")


async def test_unknown_and_malformed_case_ids_are_distinct() -> None:
    service = CaseService(InMemoryCaseRepository(), clock=lambda: NOW)
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.get(
            "/internal/support-cases/00000000-0000-0000-0000-000000000001"
        )
        malformed = await client.get("/internal/support-cases/not-a-uuid")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "case_not_found"
    assert malformed.status_code == 422


@pytest.mark.parametrize("filter_name", ["thread_id", "order_id"])
@pytest.mark.parametrize("filter_value", ["", "   "])
async def test_empty_text_filters_are_rejected_at_the_api_boundary(
    filter_name: str,
    filter_value: str,
) -> None:
    app = _app_with_service(object())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/internal/support-cases",
            params={filter_name: filter_value},
        )

    assert response.status_code == 422


async def test_storage_failure_returns_safe_service_unavailable_error() -> None:
    class UnavailableCaseService:
        async def list_cases(self, _query):
            raise CasePersistenceError("database password appeared here")

    app = _app_with_service(UnavailableCaseService())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/internal/support-cases")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "case_storage_unavailable",
            "message": "Support-case storage is temporarily unavailable.",
        }
    }
    assert "password" not in response.text
