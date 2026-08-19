"""Deterministic demo-only canonical HTTP Provider Simulator.

This module is intentionally separate from the production transport. It is
used only by local tests and development; no application lifespan or worker
starts it automatically.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response

from agent.integrations.models import ProviderCommandEnvelope, ProviderCommandResult


def create_provider_simulator(
    *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> FastAPI:
    """Create a deterministic local provider endpoint with idempotent results."""
    app = FastAPI(title="Demo Provider Simulator", docs_url=None, redoc_url=None)
    results: dict[str, ProviderCommandResult] = {}

    @app.post("/v1/commands")
    async def submit(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        command_header: str | None = Header(default=None, alias="X-Provider-Command-ID"),
        outcome: str = Header(default="accepted", alias="X-Provider-Simulator-Outcome"),
    ) -> Response:
        """Validate canonical envelopes and return a controlled local response."""
        try:
            command = ProviderCommandEnvelope.model_validate(await request.json())
        except Exception as error:
            raise HTTPException(status_code=422, detail="invalid canonical command") from error
        if idempotency_key != command.idempotency_key or command_header != str(command.command_id):
            raise HTTPException(status_code=400, detail="missing canonical idempotency headers")
        if command.idempotency_key in results:
            return Response(
                content=results[command.idempotency_key].model_dump_json(),
                media_type="application/json",
            )
        if outcome == "http_409":
            raise HTTPException(status_code=409, detail="demo conflict")
        if outcome == "http_422":
            raise HTTPException(status_code=422, detail="demo validation error")
        if outcome == "http_429":
            return Response(status_code=429, headers={"Retry-After": "2"})
        if outcome == "http_500":
            raise HTTPException(status_code=500, detail="demo transient error")
        if outcome == "timeout":
            await sleep(60.0)
        if outcome == "invalid_json":
            return Response(content="not-json", media_type="application/json")
        result = ProviderCommandResult(
            command_id=(uuid4() if outcome == "wrong_command_id" else command.command_id),
            status="rejected" if outcome == "rejected" else "accepted",
            provider_operation_id=f"demo-{command.command_id}",
            provider_reference=f"demo-ref-{command.aggregate_id}",
            received_at=datetime.now(UTC),
        )
        results[command.idempotency_key] = result
        return Response(content=result.model_dump_json(), media_type="application/json")

    return app
