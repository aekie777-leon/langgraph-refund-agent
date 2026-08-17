"""Unit tests for PostgreSQL pool lifecycle wiring."""

import pytest

from agent import webapp
from agent.cases.service import CaseService
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
        assert isinstance(configured_operations[0]["operation_service"], OperationService)
        assert cleared == []

    assert pool.calls == ["open", "wait", "close"]
    assert cleared == [True]
