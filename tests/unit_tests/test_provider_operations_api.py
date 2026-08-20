"""Unit tests for the internal Provider operations HTTP boundary."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.dependencies import require_access_scope
from agent.auth.rbac import role_permissions
from agent.auth.visibility import ForbiddenError
from agent.cases.api_errors import register_case_exception_handlers
from agent.integrations.provider_operations_api import router
from agent.integrations.provider_operations_api_errors import (
    register_provider_operations_exception_handlers,
)
from agent.integrations.provider_operations_contracts import (
    ProviderAttemptActivity,
    ProviderAttemptActivityFeed,
    ProviderInboxDetail,
    ProviderInboxQueueSummary,
    ProviderOutboxDetail,
    ProviderOutboxQueueSummary,
    ProviderQueueOverview,
    ProviderRedriveRequest,
    ProviderRedriveView,
)
from agent.integrations.provider_operations_repository import (
    ProviderOperationsConflictError,
    ProviderOperationsNotFoundError,
    ProviderOperationsPersistenceError,
)
from agent.integrations.provider_operations_runtime import (
    get_provider_operations_service,
)
from agent.integrations.provider_operations_service import ProviderOperationsService
from tests.fakes.identity import make_scope

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
COMMAND_ID = UUID("00000000-0000-4000-8000-000000000301")
INBOX_ID = UUID("00000000-0000-4000-8000-000000000302")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000000303")
SUPERVISOR_SCOPE = make_scope(
    "supervisor", tenant_id="tenant-provider-ops", user_id="supervisor-a"
)
REDRIVE_BODY = {
    "request_id": "ops:api-redrive-1",
    "reason_code": "transient_incident_resolved",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _overview() -> ProviderQueueOverview:
    return ProviderQueueOverview(
        outbox=(
            ProviderOutboxQueueSummary(status="dead", count=2, oldest_available_at=NOW),
        ),
        inbox=(
            ProviderInboxQueueSummary(
                status="failed", count=1, oldest_available_at=NOW
            ),
        ),
        generated_at=NOW,
    )


def _activity() -> ProviderAttemptActivityFeed:
    return ProviderAttemptActivityFeed(
        items=(
            ProviderAttemptActivity(
                queue="outbox",
                resource_id=COMMAND_ID,
                command_id=COMMAND_ID,
                cycle=1,
                attempt_number=1,
                outcome="retry_scheduled",
                failure_kind="http_retryable",
                http_status=500,
                safe_error_code="provider_http_500",
                started_at=NOW,
                finished_at=NOW,
            ),
        ),
        generated_at=NOW,
    )


def _redrive() -> ProviderRedriveView:
    return ProviderRedriveView(
        request_id=REDRIVE_BODY["request_id"],
        reason_code="transient_incident_resolved",
        actor=SUPERVISOR_SCOPE.identity,
        previous_cycle=1,
        new_cycle=2,
        created_at=NOW,
    )


def _outbox_detail() -> ProviderOutboxDetail:
    return ProviderOutboxDetail(
        command_id=COMMAND_ID,
        aggregate_type="order_operation",
        aggregate_id=AGGREGATE_ID,
        status="dead",
        delivery_cycle=1,
        attempts_in_cycle=1,
        available_at=NOW,
        last_failure_kind="network_error",
        last_error_code="provider_connection_error",
        created_at=NOW,
        updated_at=NOW,
        dead_at=NOW,
    )


def _inbox_detail() -> ProviderInboxDetail:
    return ProviderInboxDetail(
        inbox_id=INBOX_ID,
        command_id=COMMAND_ID,
        aggregate_type="order_operation",
        aggregate_id=AGGREGATE_ID,
        status="failed",
        processing_cycle=1,
        attempts_in_cycle=5,
        total_attempts=5,
        available_at=NOW,
        last_error_code="inbox_attempts_exhausted",
        received_at=NOW,
        updated_at=NOW,
        failed_at=NOW,
    )


def _service_mock() -> AsyncMock:
    service = AsyncMock(spec=ProviderOperationsService)
    service.get_queue_overview.return_value = _overview()
    service.get_attempt_activity.return_value = _activity()
    service.get_outbox_detail.return_value = _outbox_detail()
    service.get_inbox_detail.return_value = _inbox_detail()
    service.redrive_outbox.return_value = _redrive()
    service.redrive_inbox.return_value = _redrive()
    return service


def _app_with_service(
    service: object,
    *,
    scope=SUPERVISOR_SCOPE,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    register_provider_operations_exception_handlers(app)
    app.dependency_overrides[get_provider_operations_service] = lambda: service
    if scope is not None:
        app.dependency_overrides[require_access_scope] = lambda: scope
    return app


def test_provider_error_registration_preserves_existing_shared_forbidden_handler() -> (
    None
):
    app = FastAPI()
    register_case_exception_handlers(app)
    shared_handler = app.exception_handlers[ForbiddenError]

    register_provider_operations_exception_handlers(app)

    assert app.exception_handlers[ForbiddenError] is shared_handler


async def test_all_six_routes_delegate_exact_typed_arguments() -> None:
    service = _service_mock()
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        queues = await client.get("/internal/provider-operations/queues")
        activity = await client.get(
            "/internal/provider-operations/attempts", params={"limit": 7}
        )
        outbox = await client.get(
            f"/internal/provider-operations/outbox/{COMMAND_ID}",
            params={"history_limit": 7},
        )
        inbox = await client.get(f"/internal/provider-operations/inbox/{INBOX_ID}")
        outbox_redrive = await client.post(
            f"/internal/provider-operations/outbox/{COMMAND_ID}/redrives",
            json=REDRIVE_BODY,
        )
        inbox_redrive = await client.post(
            f"/internal/provider-operations/inbox/{INBOX_ID}/redrives",
            json=REDRIVE_BODY,
        )

    assert [
        queues.status_code,
        activity.status_code,
        outbox.status_code,
        inbox.status_code,
        outbox_redrive.status_code,
        inbox_redrive.status_code,
    ] == [200, 200, 200, 200, 200, 200]
    assert queues.json()["outbox"][0] == {
        "status": "dead",
        "count": 2,
        "oldest_available_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    assert outbox.json()["command_id"] == str(COMMAND_ID)
    assert activity.json()["items"][0] == {
        "queue": "outbox",
        "resource_id": str(COMMAND_ID),
        "command_id": str(COMMAND_ID),
        "cycle": 1,
        "attempt_number": 1,
        "outcome": "retry_scheduled",
        "failure_kind": "http_retryable",
        "http_status": 500,
        "safe_error_code": "provider_http_500",
        "started_at": NOW.isoformat().replace("+00:00", "Z"),
        "finished_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    assert inbox.json()["inbox_id"] == str(INBOX_ID)
    assert outbox_redrive.json() == inbox_redrive.json()

    service.get_queue_overview.assert_awaited_once_with(SUPERVISOR_SCOPE)
    service.get_attempt_activity.assert_awaited_once_with(
        SUPERVISOR_SCOPE, limit=7
    )
    service.get_outbox_detail.assert_awaited_once_with(
        SUPERVISOR_SCOPE, COMMAND_ID, history_limit=7
    )
    service.get_inbox_detail.assert_awaited_once_with(
        SUPERVISOR_SCOPE, INBOX_ID, history_limit=50
    )
    expected_request = ProviderRedriveRequest.model_validate(REDRIVE_BODY)
    service.redrive_outbox.assert_awaited_once_with(
        SUPERVISOR_SCOPE, COMMAND_ID, expected_request
    )
    service.redrive_inbox.assert_awaited_once_with(
        SUPERVISOR_SCOPE, INBOX_ID, expected_request
    )


@pytest.mark.parametrize(
    "scope",
    [
        make_scope("customer", tenant_id="tenant-provider-ops"),
        make_scope("support_agent", tenant_id="tenant-provider-ops"),
        make_scope("support_agent", tenant_id="tenant-provider-ops").model_copy(
            update={"permissions": role_permissions("supervisor")}
        ),
    ],
)
async def test_non_supervisor_and_forged_role_cannot_read_or_redrive(scope) -> None:
    repository = _service_mock()
    service = ProviderOperationsService(repository)
    app = _app_with_service(service, scope=scope)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        read = await client.get("/internal/provider-operations/queues")
        redrive = await client.post(
            f"/internal/provider-operations/inbox/{INBOX_ID}/redrives",
            json=REDRIVE_BODY,
        )

    assert read.status_code == 403
    assert redrive.status_code == 403
    assert read.json() == redrive.json()
    assert read.json()["error"]["code"] == "forbidden"
    repository.get_queue_overview.assert_not_awaited()
    repository.redrive_inbox.assert_not_awaited()


async def test_canonical_supervisor_can_read_and_redrive() -> None:
    repository = _service_mock()
    service = ProviderOperationsService(repository)
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        read = await client.get("/internal/provider-operations/queues")
        redrive = await client.post(
            f"/internal/provider-operations/inbox/{INBOX_ID}/redrives",
            json=REDRIVE_BODY,
        )

    assert read.status_code == 200
    assert redrive.status_code == 200
    repository.get_queue_overview.assert_awaited_once_with(SUPERVISOR_SCOPE)
    repository.redrive_inbox.assert_awaited_once()


async def test_missing_credentials_use_shared_401_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent.auth.dependencies.get_identity_provider",
        lambda: DemoIdentityProvider({}),
    )
    app = _app_with_service(_service_mock(), scope=None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/internal/provider-operations/queues")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_absent_and_cross_tenant_failures_have_identical_404_envelopes() -> None:
    service = _service_mock()
    service.get_outbox_detail.side_effect = [
        ProviderOperationsNotFoundError("absent identifier"),
        ProviderOperationsNotFoundError("cross-tenant identifier with secret"),
    ]
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        absent = await client.get(f"/internal/provider-operations/outbox/{COMMAND_ID}")
        cross_tenant = await client.get(
            f"/internal/provider-operations/outbox/{COMMAND_ID}"
        )

    assert absent.status_code == cross_tenant.status_code == 404
    assert (
        absent.json()
        == cross_tenant.json()
        == {
            "error": {
                "code": "provider_operations_not_found",
                "message": "The requested Provider operations resource does not exist.",
            }
        }
    )
    assert "secret" not in absent.text + cross_tenant.text


@pytest.mark.parametrize(
    "code",
    [
        "status_not_redrivable",
        "active_lease",
        "provider_rejection",
        "current_cycle_terminal_evidence_required",
        "technical_terminal_failure_required",
        "lease_expiry_not_attempt_exhausting",
        "redrive_state_changed",
        "request_id_conflict",
        "audit_conflict",
        "aggregate_association_mismatch",
        "aggregate_state_mismatch",
        "review_case_association_mismatch",
        "aggregate_state_changed",
    ],
)
async def test_allowlisted_conflict_codes_are_safe_and_stable(code: str) -> None:
    service = _service_mock()
    service.redrive_outbox.side_effect = ProviderOperationsConflictError(code)
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/internal/provider-operations/outbox/{COMMAND_ID}/redrives",
            json=REDRIVE_BODY,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == code


async def test_unknown_conflict_code_is_replaced_with_generic_safe_code() -> None:
    service = _service_mock()
    service.redrive_outbox.side_effect = ProviderOperationsConflictError(
        "constraint_name_contains_secret"
    )
    app = _app_with_service(service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/internal/provider-operations/outbox/{COMMAND_ID}/redrives",
            json=REDRIVE_BODY,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_operations_conflict"
    assert "constraint" not in response.text
    assert "secret" not in response.text


async def test_forbidden_and_persistence_errors_never_reflect_internal_details() -> (
    None
):
    forbidden_service = _service_mock()
    forbidden_service.get_queue_overview.side_effect = ForbiddenError(
        "payload contains provider secret"
    )
    persistence_service = _service_mock()
    persistence_service.get_queue_overview.side_effect = (
        ProviderOperationsPersistenceError(
            "SQL constraint provider_connection_secret payload"
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app_with_service(forbidden_service)),
        base_url="http://test",
    ) as client:
        forbidden = await client.get("/internal/provider-operations/queues")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app_with_service(persistence_service)),
        base_url="http://test",
    ) as client:
        unavailable = await client.get("/internal/provider-operations/queues")

    assert forbidden.status_code == 403
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "error": {
            "code": "provider_operations_storage_unavailable",
            "message": "Provider operations storage is temporarily unavailable.",
        }
    }
    combined = forbidden.text + unavailable.text
    assert "payload" not in combined
    assert "constraint" not in combined
    assert "secret" not in combined


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/internal/provider-operations/outbox/not-a-uuid", None),
        (
            "GET",
            f"/internal/provider-operations/outbox/{COMMAND_ID}?history_limit=0",
            None,
        ),
        (
            "POST",
            f"/internal/provider-operations/outbox/{COMMAND_ID}/redrives",
            {
                **REDRIVE_BODY,
                "payload": "top-secret-payload-value",
                "tenant_id": "secret-tenant-value",
            },
        ),
        (
            "POST",
            f"/internal/provider-operations/inbox/{INBOX_ID}/redrives",
            {"request_id": "invalid secret request", "reason_code": "free-form-secret"},
        ),
    ],
)
async def test_invalid_inputs_return_sanitized_422(
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    app = _app_with_service(_service_mock())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, path, json=body)

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "provider_operations_request_invalid",
            "message": "The Provider operations request is invalid.",
        }
    }
    assert "top-secret-payload-value" not in response.text
    assert "secret-tenant-value" not in response.text
    assert "invalid secret request" not in response.text
    assert "free-form-secret" not in response.text


async def test_history_limit_upper_bound_is_rejected() -> None:
    app = _app_with_service(_service_mock())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/internal/provider-operations/inbox/{INBOX_ID}",
            params={"history_limit": 101},
        )

    assert response.status_code == 422


async def test_activity_limit_is_bounded_and_sanitized() -> None:
    app = _app_with_service(_service_mock())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/internal/provider-operations/attempts",
            params={"limit": 101, "tenant_id": "ignored-secret-tenant"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_operations_request_invalid"
    assert "ignored-secret-tenant" not in response.text


def test_openapi_has_exact_safe_provider_operations_surface() -> None:
    schema = _app_with_service(_service_mock()).openapi()
    assert set(schema["paths"]) == {
        "/internal/provider-operations/queues",
        "/internal/provider-operations/attempts",
        "/internal/provider-operations/outbox/{command_id}",
        "/internal/provider-operations/inbox/{inbox_id}",
        "/internal/provider-operations/outbox/{command_id}/redrives",
        "/internal/provider-operations/inbox/{inbox_id}/redrives",
    }
    assert set(schema["paths"]["/internal/provider-operations/queues"]) == {"get"}
    assert set(schema["paths"]["/internal/provider-operations/attempts"]) == {"get"}
    assert set(
        schema["paths"]["/internal/provider-operations/outbox/{command_id}"]
    ) == {"get"}
    assert set(schema["paths"]["/internal/provider-operations/inbox/{inbox_id}"]) == {
        "get"
    }
    assert set(
        schema["paths"]["/internal/provider-operations/outbox/{command_id}/redrives"]
    ) == {"post"}
    assert set(
        schema["paths"]["/internal/provider-operations/inbox/{inbox_id}/redrives"]
    ) == {"post"}

    serialized = json.dumps(schema, sort_keys=True)
    assert "tenant_id" not in serialized
    request_schema = schema["components"]["schemas"]["ProviderRedriveRequest"]
    assert set(request_schema["properties"]) == {"request_id", "reason_code"}
    sensitive_properties = {
        "payload",
        "customer_id",
        "source_message_id",
        "provider_connection_id",
        "provider_operation_id",
        "provider_reference",
        "raw_body_sha256",
        "signature",
        "secret",
        "last_error_message",
        "safe_error_message",
        "idempotency_key",
    }
    for component in schema["components"]["schemas"].values():
        assert set(component.get("properties", ())).isdisjoint(sensitive_properties)

    queues_responses = schema["paths"]["/internal/provider-operations/queues"]["get"][
        "responses"
    ]
    detail_responses = schema["paths"][
        "/internal/provider-operations/outbox/{command_id}"
    ]["get"]["responses"]
    redrive_responses = schema["paths"][
        "/internal/provider-operations/outbox/{command_id}/redrives"
    ]["post"]["responses"]
    assert {"200", "401", "403", "503"}.issubset(queues_responses)
    assert {"200", "401", "403", "404", "422", "503"}.issubset(detail_responses)
    assert {"200", "401", "403", "404", "409", "422", "503"}.issubset(redrive_responses)
