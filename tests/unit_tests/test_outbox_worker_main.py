"""Unit coverage for standalone Outbox Worker process assembly."""

import asyncio
from typing import Any

import pytest

from agent.integrations import worker_main

pytestmark = pytest.mark.anyio


def _patch_assembly(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolver_error: BaseException | None = None,
    wait_error: BaseException | None = None,
    client_error: BaseException | None = None,
    worker_construction_error: BaseException | None = None,
    worker_error: BaseException | None = None,
) -> tuple[list[str], dict[str, object]]:
    calls: list[str] = []
    captured: dict[str, object] = {}

    def load_environment() -> None:
        calls.append("dotenv")

    class FakeResolver:
        @classmethod
        def from_environment(cls, *, allow_insecure_http: bool) -> object:
            calls.append("resolver")
            captured["allow_insecure_http"] = allow_insecure_http
            if resolver_error is not None:
                raise resolver_error
            return cls()

    class FakePool:
        async def open(self) -> None:
            calls.append("open")

        async def wait(self) -> None:
            calls.append("wait")
            if wait_error is not None:
                raise wait_error

        async def close(self) -> None:
            calls.append("close")

    pool = FakePool()

    def create_pool() -> FakePool:
        calls.append("create")
        return pool

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            calls.append("client_enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            calls.append("client_exit")

    def create_client(*, follow_redirects: bool) -> FakeClient:
        calls.append("client")
        captured["follow_redirects"] = follow_redirects
        if client_error is not None:
            raise client_error
        return FakeClient()

    class FakeRepository:
        def __init__(self, received_pool: FakePool) -> None:
            calls.append("repository")
            captured["repository_pool"] = received_pool
            captured["repository"] = self

    class FakeFinalizer:
        def __init__(self, received_pool: FakePool) -> None:
            calls.append("finalizer")
            captured["finalizer_pool"] = received_pool
            captured["finalizer"] = self

    class FakeTransport:
        def __init__(self, client: FakeClient) -> None:
            calls.append("transport")
            captured["transport_client"] = client
            captured["transport"] = self

    class FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            calls.append("worker")
            if worker_construction_error is not None:
                raise worker_construction_error
            captured["worker_arguments"] = kwargs

        async def run_forever(self) -> None:
            calls.append("run_forever")
            if worker_error is not None:
                raise worker_error

    monkeypatch.setattr(worker_main, "load_dotenv", load_environment)
    monkeypatch.setattr(
        worker_main, "EnvironmentProviderConnectionResolver", FakeResolver
    )
    monkeypatch.setattr(worker_main, "create_async_connection_pool", create_pool)
    monkeypatch.setattr(worker_main.httpx, "AsyncClient", create_client)
    monkeypatch.setattr(worker_main, "PostgresIntegrationRepository", FakeRepository)
    monkeypatch.setattr(worker_main, "PostgresOutboxFinalizer", FakeFinalizer)
    monkeypatch.setattr(worker_main, "CanonicalHttpProviderTransport", FakeTransport)
    monkeypatch.setattr(worker_main, "OutboxDispatchWorker", FakeWorker)
    return calls, captured


async def test_run_assembles_explicit_worker_id_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, captured = _patch_assembly(monkeypatch)
    monkeypatch.setenv("OUTBOX_WORKER_ID", "outbox-explicit-id")
    monkeypatch.setenv("PROVIDER_ALLOW_INSECURE_HTTP", "true")

    await worker_main.run()

    assert calls == [
        "dotenv",
        "resolver",
        "create",
        "open",
        "wait",
        "client",
        "client_enter",
        "repository",
        "transport",
        "finalizer",
        "worker",
        "run_forever",
        "client_exit",
        "close",
    ]
    assert captured["allow_insecure_http"] is True
    assert captured["follow_redirects"] is False
    assert captured["repository_pool"] is captured["finalizer_pool"]
    arguments = captured["worker_arguments"]
    assert isinstance(arguments, dict)
    assert arguments["repository"] is captured["repository"]
    assert arguments["transport"] is captured["transport"]
    assert arguments["finalizer"] is captured["finalizer"]
    assert arguments["worker_id"] == "outbox-explicit-id"


@pytest.mark.parametrize("configured_worker_id", [None, ""])
async def test_run_defaults_worker_id_for_missing_or_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
    configured_worker_id: str | None,
) -> None:
    _calls, captured = _patch_assembly(monkeypatch)
    monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "test-host")
    if configured_worker_id is None:
        monkeypatch.delenv("OUTBOX_WORKER_ID", raising=False)
    else:
        monkeypatch.setenv("OUTBOX_WORKER_ID", configured_worker_id)

    await worker_main.run()

    arguments = captured["worker_arguments"]
    assert isinstance(arguments, dict)
    assert arguments["worker_id"] == "outbox-test-host"


async def test_run_does_not_create_a_pool_when_resolver_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _patch_assembly(
        monkeypatch, resolver_error=RuntimeError("resolver failed")
    )

    with pytest.raises(RuntimeError, match="resolver failed"):
        await worker_main.run()

    assert calls == ["dotenv", "resolver"]


async def test_run_closes_pool_when_wait_fails_before_client_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _patch_assembly(monkeypatch, wait_error=RuntimeError("pool failed"))

    with pytest.raises(RuntimeError, match="pool failed"):
        await worker_main.run()

    assert calls == ["dotenv", "resolver", "create", "open", "wait", "close"]


@pytest.mark.parametrize(
    ("client_error", "worker_construction_error", "expected_tail"),
    [
        (RuntimeError("client failed"), None, ["client", "close"]),
        (
            None,
            RuntimeError("worker construction failed"),
            ["worker", "client_exit", "close"],
        ),
    ],
)
async def test_run_closes_open_resources_when_client_or_worker_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
    client_error: BaseException | None,
    worker_construction_error: BaseException | None,
    expected_tail: list[str],
) -> None:
    calls, _ = _patch_assembly(
        monkeypatch,
        client_error=client_error,
        worker_construction_error=worker_construction_error,
    )
    error = client_error or worker_construction_error
    assert error is not None

    with pytest.raises(type(error), match=str(error)):
        await worker_main.run()

    assert calls[-len(expected_tail) :] == expected_tail


@pytest.mark.parametrize(
    "worker_error", [RuntimeError("worker failed"), asyncio.CancelledError()]
)
async def test_run_closes_resources_and_propagates_worker_failures(
    monkeypatch: pytest.MonkeyPatch,
    worker_error: BaseException,
) -> None:
    calls, _ = _patch_assembly(monkeypatch, worker_error=worker_error)

    with pytest.raises(type(worker_error)):
        await worker_main.run()

    assert calls[-3:] == ["run_forever", "client_exit", "close"]


def test_main_only_delegates_to_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run() -> None:
        calls.append("run")

    def fake_asyncio_run(awaitable: object) -> None:
        calls.append("asyncio.run")
        assert hasattr(awaitable, "close")
        awaitable.close()  # type: ignore[union-attr]

    monkeypatch.setattr(worker_main, "run", fake_run)
    monkeypatch.setattr(worker_main.asyncio, "run", fake_asyncio_run)

    worker_main.main()

    assert calls == ["asyncio.run"]
