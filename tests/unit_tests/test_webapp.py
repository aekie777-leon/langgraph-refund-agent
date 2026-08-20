"""Unit tests for PostgreSQL pool lifecycle and webhook runtime wiring."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import webapp
from agent.cases.service import CaseService
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.provider_operations_postgres import (
    PostgresProviderOperationsRepository,
)
from agent.integrations.provider_operations_service import ProviderOperationsService
from agent.integrations.webhook_adapter import CanonicalHmacWebhookAdapter
from agent.integrations.webhook_resolver import (
    EnvironmentProviderWebhookConnectionResolver,
)
from agent.operations.service import OperationService

pytestmark = pytest.mark.anyio


class FakePool:
    """Record lifecycle calls without opening PostgreSQL connections."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def open(self) -> None:
        self.calls.append("open")

    async def wait(self) -> None:
        self.calls.append("wait")

    async def close(self) -> None:
        self.calls.append("close")


def _patch_identity_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str] | None = None,
) -> None:
    async def initialize(*_args, **_kwargs) -> None:
        if calls is not None:
            calls.append("identity_open")

    async def shutdown() -> None:
        if calls is not None:
            calls.append("identity_close")

    monkeypatch.setattr(webapp, "initialize_identity_runtime", initialize)
    monkeypatch.setattr(webapp, "shutdown_identity_runtime", shutdown)
    monkeypatch.setattr(
        webapp,
        "get_identity_runtime",
        lambda: SimpleNamespace(directory=None),
    )


async def test_lifespan_opens_configures_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    configured: list[CaseService] = []
    configured_operations: list[dict[str, object]] = []
    cleared: list[bool] = []
    identity_calls: list[str] = []
    _patch_identity_lifecycle(monkeypatch, identity_calls)
    monkeypatch.setattr(webapp, "create_async_connection_pool", lambda: pool)
    monkeypatch.setattr(
        webapp,
        "configure_case_service",
        configured.append,
    )
    monkeypatch.setattr(
        webapp,
        "configure_operation_dependencies",
        lambda **kwargs: configured_operations.append(kwargs),
    )
    monkeypatch.setattr(webapp, "clear_operation_dependencies", lambda: None)
    monkeypatch.setattr(
        webapp,
        "clear_case_service",
        lambda: cleared.append(True),
    )

    async with webapp.lifespan(webapp.app):
        assert pool.calls == ["open", "wait"]
        assert len(configured) == 1
        assert isinstance(configured[0], CaseService)
        assert len(configured_operations) == 1
        assert isinstance(
            configured_operations[0]["operation_service"], OperationService
        )
        assert isinstance(
            webapp.app.state.integration_repository, PostgresIntegrationRepository
        )
        assert webapp.app.state.integration_repository._pool is pool
        assert isinstance(
            webapp.app.state.provider_webhook_resolver,
            EnvironmentProviderWebhookConnectionResolver,
        )
        assert isinstance(
            webapp.app.state.provider_webhook_adapter, CanonicalHmacWebhookAdapter
        )
        provider_operations_service = webapp.app.state.provider_operations_service
        assert isinstance(provider_operations_service, ProviderOperationsService)
        assert isinstance(
            provider_operations_service._repository,
            PostgresProviderOperationsRepository,
        )
        assert provider_operations_service._repository._pool is pool
        assert cleared == []

    assert pool.calls == ["open", "wait", "close"]
    assert cleared == [True]
    assert identity_calls == ["identity_open", "identity_close"]
    assert not hasattr(webapp.app.state, "provider_operations_service")


def test_production_app_registers_the_provider_webhook_route() -> None:
    assert any(
        getattr(route, "path", None) == "/webhooks/providers/{provider_connection_id}"
        and "POST" in getattr(route, "methods", set())
        for included_router in webapp.app.routes
        for route in getattr(
            getattr(included_router, "original_router", included_router), "routes", []
        )
    )


def test_production_app_reports_v090() -> None:
    assert webapp.app.version == "0.9.0"


def test_production_app_preserves_case_routes_and_adds_exact_provider_ops_routes() -> (
    None
):
    paths = {
        getattr(route, "path", None)
        for included_router in webapp.app.routes
        for route in getattr(
            getattr(included_router, "original_router", included_router), "routes", []
        )
    }

    assert "/internal/support-cases" in paths
    assert "/internal/support-cases/{case_id}" in paths
    assert {
        "/internal/provider-operations/queues",
        "/internal/provider-operations/outbox/{command_id}",
        "/internal/provider-operations/inbox/{inbox_id}",
        "/internal/provider-operations/outbox/{command_id}/redrives",
        "/internal/provider-operations/inbox/{inbox_id}/redrives",
    }.issubset(paths)


async def test_lifespan_closes_an_open_pool_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPool(FakePool):
        async def wait(self) -> None:
            self.calls.append("wait")
            raise RuntimeError("pool initialization failed")

    pool = FailingPool()
    identity_calls: list[str] = []
    _patch_identity_lifecycle(monkeypatch, identity_calls)
    monkeypatch.setattr(webapp, "create_async_connection_pool", lambda: pool)

    with pytest.raises(RuntimeError, match="pool initialization failed"):
        async with webapp.lifespan(webapp.app):
            pass

    assert pool.calls == ["open", "wait", "close"]
    assert identity_calls == ["identity_open", "identity_close"]


async def test_lifespan_clears_partial_runtime_configuration_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    identity_calls: list[str] = []
    cleared: list[str] = []
    _patch_identity_lifecycle(monkeypatch, identity_calls)
    monkeypatch.setattr(webapp, "create_async_connection_pool", lambda: pool)
    monkeypatch.setattr(webapp, "configure_case_service", lambda _service: None)

    def fail_operation_configuration(**_kwargs) -> None:
        raise RuntimeError("operation configuration failed")

    monkeypatch.setattr(
        webapp,
        "configure_operation_dependencies",
        fail_operation_configuration,
    )
    monkeypatch.setattr(
        webapp,
        "clear_case_service",
        lambda: cleared.append("case"),
    )

    with pytest.raises(RuntimeError, match="operation configuration failed"):
        async with webapp.lifespan(webapp.app):
            pass

    assert cleared == ["case"]
    assert pool.calls == ["open", "wait", "close"]
    assert identity_calls == ["identity_open", "identity_close"]


async def test_lifespan_closes_an_open_pool_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    identity_calls: list[str] = []
    _patch_identity_lifecycle(monkeypatch, identity_calls)
    monkeypatch.setattr(webapp, "create_async_connection_pool", lambda: pool)
    monkeypatch.setattr(webapp, "configure_case_service", lambda _service: None)
    monkeypatch.setattr(
        webapp, "configure_operation_dependencies", lambda **_kwargs: None
    )
    monkeypatch.setattr(webapp, "configure_refund_service", lambda _service: None)
    monkeypatch.setattr(webapp, "clear_case_service", lambda: None)
    monkeypatch.setattr(webapp, "clear_operation_dependencies", lambda: None)
    monkeypatch.setattr(webapp, "clear_refund_service", lambda: None)

    with pytest.raises(asyncio.CancelledError):
        async with webapp.lifespan(webapp.app):
            raise asyncio.CancelledError()

    assert pool.calls == ["open", "wait", "close"]
    assert identity_calls == ["identity_open", "identity_close"]
