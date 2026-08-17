"""Configure application lifecycle resources for LangGraph API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.cases.api import router as support_case_router
from agent.cases.api_errors import register_case_exception_handlers
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.cases.runtime import clear_case_service, configure_case_service
from agent.cases.service import CaseService
from agent.database import create_async_connection_pool
from agent.operations.demo_provider import DemoOrderProvider
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from agent.operations.runtime import (
    clear_operation_dependencies,
    configure_operation_dependencies,
)
from agent.operations.service import OperationService
from agent.refunds.postgres_repository import PostgresRefundRepository
from agent.refunds.runtime import clear_refund_service, configure_refund_service
from agent.refunds.service import RefundService


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Open and close the application-scoped PostgreSQL connection pool."""
    pool = create_async_connection_pool()
    await pool.open()
    try:
        await pool.wait()
        configure_case_service(CaseService(PostgresCaseRepository(pool)))
        configure_operation_dependencies(
            order_provider=DemoOrderProvider(),
            operation_service=OperationService(PostgresOrderOperationRepository(pool)),
        )
        configure_refund_service(RefundService(PostgresRefundRepository(pool)))
        try:
            yield
        finally:
            clear_refund_service()
            clear_operation_dependencies()
            clear_case_service()
    finally:
        await pool.close()


app = FastAPI(
    title="OpsPilot Internal API",
    version="0.6.0",
    lifespan=lifespan,
)
app.include_router(support_case_router)
register_case_exception_handlers(app)
