"""Unit tests for PostgreSQL pool lifecycle and webhook runtime wiring."""

import asyncio

import pytest

from agent import webapp
from agent.cases.service import CaseService
from agent.integrations.postgres_repository import PostgresIntegrationRepository
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


async def test_lifespan_opens_configures_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    configured: list[CaseService] = []
    configured_operations: list[dict[str, object]] = []
    cleared: list[bool] = []
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
        assert cleared == []

    assert pool.calls == ["open", "wait", "close"]
    assert cleared == [True]


def test_production_app_registers_the_provider_webhook_route() -> None:
    assert any(
        getattr(route, "path", None) == "/webhooks/providers/{provider_connection_id}"
        and "POST" in getattr(route, "methods", set())
        for included_router in webapp.app.routes
        for route in getattr(
            getattr(included_router, "original_router", included_router), "routes", []
        )
    )


async def test_lifespan_closes_an_open_pool_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPool(FakePool):
        async def wait(self) -> None:
            self.calls.append("wait")
            raise RuntimeError("pool initialization failed")

    pool = FailingPool()
    monkeypatch.setattr(webapp, "create_async_connection_pool", lambda: pool)

    with pytest.raises(RuntimeError, match="pool initialization failed"):
        async with webapp.lifespan(webapp.app):
            pass

    assert pool.calls == ["open", "wait", "close"]


async def test_lifespan_closes_an_open_pool_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
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
