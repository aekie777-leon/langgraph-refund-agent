"""Configure application lifecycle resources for LangGraph API."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.cases.api import router as support_case_router
from agent.cases.api_errors import register_case_exception_handlers
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.cases.runtime import clear_case_service, configure_case_service
from agent.cases.service import CaseService
from agent.database import create_async_connection_pool
from agent.integrations.connection_resolver import EnvironmentProviderConnectionResolver
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.provider_failure import PostgresProviderQueueFailureCoordinator
from agent.integrations.provider_operations_api import (
    router as provider_operations_router,
)
from agent.integrations.provider_operations_api_errors import (
    register_provider_operations_exception_handlers,
)
from agent.integrations.provider_operations_postgres import (
    PostgresProviderOperationsRepository,
)
from agent.integrations.provider_operations_service import ProviderOperationsService
from agent.integrations.webhook_adapter import CanonicalHmacWebhookAdapter
from agent.integrations.webhook_resolver import (
    EnvironmentProviderWebhookConnectionResolver,
)
from agent.integrations.webhook_router import router as provider_webhook_router
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
        _app.state.integration_repository = PostgresIntegrationRepository(pool)
        _app.state.provider_operations_service = ProviderOperationsService(
            PostgresProviderOperationsRepository(pool)
        )
        _app.state.provider_webhook_resolver = (
            EnvironmentProviderWebhookConnectionResolver.from_environment()
        )
        _app.state.provider_webhook_adapter = CanonicalHmacWebhookAdapter()
        configure_case_service(CaseService(PostgresCaseRepository(pool)))
        configure_operation_dependencies(
            order_provider=DemoOrderProvider(),
            operation_service=OperationService(
                PostgresOrderOperationRepository(pool),
                provider_queue_failure_coordinator=PostgresProviderQueueFailureCoordinator(
                    pool
                ),
            ),
            provider_connection_resolver=(
                EnvironmentProviderConnectionResolver.from_environment(
                    allow_insecure_http=os.getenv("PROVIDER_ALLOW_INSECURE_HTTP")
                    == "true"
                )
                if os.getenv("PROVIDER_CONNECTIONS_JSON")
                else None
            ),
        )
        configure_refund_service(RefundService(PostgresRefundRepository(pool)))
        try:
            yield
        finally:
            clear_refund_service()
            clear_operation_dependencies()
            clear_case_service()
    finally:
        if hasattr(_app.state, "provider_operations_service"):
            delattr(_app.state, "provider_operations_service")
        await pool.close()


app = FastAPI(
    title="OpsPilot Internal API",
    version="0.8.0",
    lifespan=lifespan,
)
app.include_router(support_case_router)
app.include_router(provider_operations_router)
app.include_router(provider_webhook_router)
register_case_exception_handlers(app)
register_provider_operations_exception_handlers(app)
