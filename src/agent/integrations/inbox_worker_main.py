"""Run the v0.7 Inbox worker as a standalone process."""

import asyncio
import os
import socket

from dotenv import load_dotenv

from agent.database import create_async_connection_pool
from agent.integrations.inbox_postgres_finalizer import PostgresInboxFinalizer
from agent.integrations.inbox_worker import InboxProcessingWorker
from agent.integrations.postgres_repository import PostgresIntegrationRepository


async def run() -> None:
    """Open process-owned resources and run Inbox processing until cancelled."""
    load_dotenv()
    pool = create_async_connection_pool()
    await pool.open()
    try:
        await pool.wait()
        worker = InboxProcessingWorker(
            repository=PostgresIntegrationRepository(pool),
            finalizer=PostgresInboxFinalizer(pool),
            worker_id=os.getenv("INBOX_WORKER_ID") or f"inbox-{socket.gethostname()}",
        )
        await worker.run_forever()
    finally:
        await pool.close()


def main() -> None:
    """Start the standalone Inbox worker without FastAPI or LangGraph."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
