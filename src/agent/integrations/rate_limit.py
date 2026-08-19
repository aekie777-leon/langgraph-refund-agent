"""Per-process provider connection concurrency and request-rate gates."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from typing import AsyncIterator

from agent.integrations.models import ProviderConnection

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class ConnectionExecutionGate:
    """Share concurrency and optional RPS enforcement by connection id."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        requests_per_second: float | None,
        clock: Clock = monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Create a gate with injectable time hooks for deterministic tests."""
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._requests_per_second = requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_request_at = 0.0
        self._rate_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Wait for a local slot and yield without holding the rate lock."""
        async with self._semaphore:
            await self._wait_for_rate_slot()
            yield

    async def _wait_for_rate_slot(self) -> None:
        """Reserve one monotonically ordered request start time."""
        if self._requests_per_second is None:
            return
        interval = 1.0 / self._requests_per_second
        async with self._rate_lock:
            now = self._clock()
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + interval
        delay = scheduled - self._clock()
        if delay > 0:
            await self._sleep(delay)


class ConnectionGateRegistry:
    """Create one execution gate per configured connection id in this process."""

    def __init__(self, *, clock: Clock = monotonic, sleep: Sleep = asyncio.sleep) -> None:
        """Initialize an empty application-scoped gate registry."""
        self._clock = clock
        self._sleep = sleep
        self._gates: dict[str, ConnectionExecutionGate] = {}

    def for_connection(self, connection: ProviderConnection) -> ConnectionExecutionGate:
        """Return the shared gate for one immutable connection configuration."""
        gate = self._gates.get(connection.connection_id)
        if gate is None:
            gate = ConnectionExecutionGate(
                max_concurrency=connection.max_concurrency,
                requests_per_second=connection.requests_per_second,
                clock=self._clock,
                sleep=self._sleep,
            )
            self._gates[connection.connection_id] = gate
        return gate
