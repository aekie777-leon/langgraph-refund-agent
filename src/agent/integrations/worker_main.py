"""Run the v0.7 outbound dispatch worker as a standalone process."""

import asyncio
import os
import socket

import httpx
from dotenv import load_dotenv

from agent.database import create_async_connection_pool
from agent.integrations.connection_resolver import EnvironmentProviderConnectionResolver
from agent.integrations.finalization import PostgresOutboxFinalizer
from agent.integrations.http_adapter import CanonicalHttpProviderTransport
from agent.integrations.outbox_worker import OutboxDispatchWorker
from agent.integrations.postgres_repository import PostgresIntegrationRepository


async def run() -> None:
    """Open process-owned resources and run outbound dispatch until cancelled."""
    load_dotenv()
    resolver = EnvironmentProviderConnectionResolver.from_environment(
        allow_insecure_http=os.getenv("PROVIDER_ALLOW_INSECURE_HTTP") == "true"
    )
    pool = create_async_connection_pool()
    await pool.open()
    try:
        await pool.wait()
        async with httpx.AsyncClient(follow_redirects=False) as client:
            worker = OutboxDispatchWorker(
                repository=PostgresIntegrationRepository(pool),
                connection_lookup=resolver,
                transport=CanonicalHttpProviderTransport(client),
                finalizer=PostgresOutboxFinalizer(pool),
                worker_id=os.getenv("OUTBOX_WORKER_ID")
                or f"outbox-{socket.gethostname()}",
            )
            await worker.run_forever()
    finally:
        await pool.close()


def main() -> None:
    """Start the standalone worker without FastAPI or LangGraph dependencies."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
