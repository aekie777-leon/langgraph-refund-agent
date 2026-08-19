"""Deterministic tests for per-process connection execution gates."""

import asyncio

import pytest

from agent.integrations.models import ProviderAuthentication, ProviderConnection
from agent.integrations.rate_limit import ConnectionGateRegistry

pytestmark = pytest.mark.anyio


def _connection(connection_id: str) -> ProviderConnection:
    return ProviderConnection(
        connection_id=connection_id, tenant_id="tenant-demo", capability="order_operation",
        base_url="https://provider.example.test", endpoint="/v1/commands",
        authentication=ProviderAuthentication(scheme="none"), max_concurrency=1,
    )


async def test_same_connection_shares_semaphore_but_other_connections_do_not() -> None:
    registry = ConnectionGateRegistry()
    one, two = _connection("same"), _connection("other")
    assert registry.for_connection(one) is registry.for_connection(one)
    assert registry.for_connection(one) is not registry.for_connection(two)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with registry.for_connection(one).acquire():
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold())
    await entered.wait()
    second = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)


async def test_rps_uses_injected_monotonic_clock_and_sleep() -> None:
    now = [0.0]
    delays: list[float] = []
    async def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay
    connection = _connection("limited").model_copy(update={"requests_per_second": 2.0})
    registry = ConnectionGateRegistry(clock=lambda: now[0], sleep=sleep)
    async with registry.for_connection(connection).acquire():
        pass
    async with registry.for_connection(connection).acquire():
        pass
    assert delays == [0.5]
