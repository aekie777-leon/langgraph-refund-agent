"""Unit tests for Provider operations service authorization and delegation."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent.auth.rbac import role_permissions
from agent.auth.visibility import ForbiddenError
from agent.integrations.provider_operations_contracts import ProviderRedriveRequest
from agent.integrations.provider_operations_service import ProviderOperationsService
from tests.fakes.identity import make_scope

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _repository() -> AsyncMock:
    repository = AsyncMock()
    repository.get_queue_overview.return_value = object()
    repository.get_outbox_detail.return_value = object()
    repository.get_inbox_detail.return_value = object()
    repository.redrive_outbox.return_value = object()
    repository.redrive_inbox.return_value = object()
    return repository


async def test_service_delegates_reads_and_redrives_for_canonical_supervisor() -> None:
    repository = _repository()
    service = ProviderOperationsService(repository)
    scope = make_scope("supervisor", tenant_id="tenant-a", user_id="supervisor-a")
    command_id = uuid4()
    inbox_id = uuid4()
    request = ProviderRedriveRequest(
        request_id="ops:req-1", reason_code="transient_incident_resolved"
    )

    assert (
        await service.get_queue_overview(scope)
        is repository.get_queue_overview.return_value
    )
    assert (
        await service.get_outbox_detail(scope, command_id, history_limit=7)
        is repository.get_outbox_detail.return_value
    )
    assert (
        await service.get_inbox_detail(scope, inbox_id, history_limit=9)
        is repository.get_inbox_detail.return_value
    )
    assert (
        await service.redrive_outbox(scope, command_id, request)
        is repository.redrive_outbox.return_value
    )
    assert (
        await service.redrive_inbox(scope, inbox_id, request)
        is repository.redrive_inbox.return_value
    )

    repository.get_outbox_detail.assert_awaited_once_with(
        scope, command_id, history_limit=7
    )
    repository.get_inbox_detail.assert_awaited_once_with(
        scope, inbox_id, history_limit=9
    )


@pytest.mark.parametrize(
    ("operation", "scope"),
    [
        (
            "read",
            make_scope("support_agent", tenant_id="tenant-a").model_copy(
                update={"permissions": role_permissions("supervisor")}
            ),
        ),
        (
            "read",
            make_scope("supervisor", tenant_id="tenant-a").model_copy(
                update={"permissions": frozenset({"provider_ops:redrive"})}
            ),
        ),
        (
            "redrive",
            make_scope("supervisor", tenant_id="tenant-a").model_copy(
                update={"permissions": frozenset({"provider_ops:read"})}
            ),
        ),
    ],
)
async def test_service_fails_closed_on_role_or_permission_mismatch(
    operation: str, scope
) -> None:
    repository = _repository()
    service = ProviderOperationsService(repository)

    with pytest.raises(ForbiddenError):
        if operation == "read":
            await service.get_queue_overview(scope)
        else:
            await service.redrive_inbox(
                scope,
                uuid4(),
                ProviderRedriveRequest(
                    request_id="ops:req-1", reason_code="manual_retry_approved"
                ),
            )

    repository.get_queue_overview.assert_not_awaited()
    repository.redrive_inbox.assert_not_awaited()
