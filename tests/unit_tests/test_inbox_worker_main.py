"""Unit coverage for the standalone Inbox Worker process assembly."""

import asyncio
from typing import Any

import pytest

from agent.integrations import inbox_worker_main

pytestmark = pytest.mark.anyio


def _patch_assembly(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wait_error: BaseException | None = None,
    worker_error: BaseException | None = None,
) -> tuple[list[str], dict[str, object]]:
    calls: list[str] = []
    captured: dict[str, object] = {}

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

    def load_environment() -> None:
        calls.append("dotenv")

    def create_pool() -> FakePool:
        calls.append("create")
        return pool

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

    class FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            calls.append("worker")
            captured["worker_arguments"] = kwargs

        async def run_forever(self) -> None:
            calls.append("run_forever")
            if worker_error is not None:
                raise worker_error

    monkeypatch.setattr(inbox_worker_main, "load_dotenv", load_environment)
    monkeypatch.setattr(inbox_worker_main, "create_async_connection_pool", create_pool)
    monkeypatch.setattr(
        inbox_worker_main, "PostgresIntegrationRepository", FakeRepository
    )
    monkeypatch.setattr(inbox_worker_main, "PostgresInboxFinalizer", FakeFinalizer)
    monkeypatch.setattr(inbox_worker_main, "InboxProcessingWorker", FakeWorker)
    return calls, captured


async def test_run_assembles_explicit_worker_id_and_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, captured = _patch_assembly(monkeypatch)
    monkeypatch.setenv("INBOX_WORKER_ID", "inbox-test-worker")

    await inbox_worker_main.run()

    assert calls == [
        "dotenv",
        "create",
        "open",
        "wait",
        "repository",
        "finalizer",
        "worker",
        "run_forever",
        "close",
    ]
    assert captured["repository_pool"] is captured["finalizer_pool"]
    arguments = captured["worker_arguments"]
    assert isinstance(arguments, dict)
    assert arguments["repository"] is captured["repository"]
    assert arguments["finalizer"] is captured["finalizer"]
    assert arguments["worker_id"] == "inbox-test-worker"


@pytest.mark.parametrize("configured_worker_id", [None, ""])
async def test_run_defaults_worker_id_for_missing_or_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
    configured_worker_id: str | None,
) -> None:
    _, captured = _patch_assembly(monkeypatch)
    monkeypatch.setattr(inbox_worker_main.socket, "gethostname", lambda: "test-host")
    if configured_worker_id is None:
        monkeypatch.delenv("INBOX_WORKER_ID", raising=False)
    else:
        monkeypatch.setenv("INBOX_WORKER_ID", configured_worker_id)

    await inbox_worker_main.run()

    arguments = captured["worker_arguments"]
    assert isinstance(arguments, dict)
    assert arguments["worker_id"] == "inbox-test-host"


async def test_run_closes_pool_and_propagates_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _patch_assembly(
        monkeypatch,
        worker_error=RuntimeError("worker failed"),
    )

    with pytest.raises(RuntimeError, match="worker failed"):
        await inbox_worker_main.run()

    assert calls[-2:] == ["run_forever", "close"]


async def test_run_closes_pool_and_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _patch_assembly(
        monkeypatch,
        worker_error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await inbox_worker_main.run()

    assert calls[-2:] == ["run_forever", "close"]


async def test_run_closes_pool_when_wait_fails_before_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _patch_assembly(
        monkeypatch,
        wait_error=RuntimeError("pool unavailable"),
    )

    with pytest.raises(RuntimeError, match="pool unavailable"):
        await inbox_worker_main.run()

    assert calls == ["dotenv", "create", "open", "wait", "close"]


async def test_run_closes_pool_when_dependency_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _patch_assembly(monkeypatch)

    def failing_repository(_: object) -> None:
        calls.append("repository")
        raise RuntimeError("repository construction failed")

    monkeypatch.setattr(
        inbox_worker_main,
        "PostgresIntegrationRepository",
        failing_repository,
    )

    with pytest.raises(RuntimeError, match="repository construction failed"):
        await inbox_worker_main.run()

    assert calls == ["dotenv", "create", "open", "wait", "repository", "close"]


def test_main_only_delegates_to_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run() -> None:
        calls.append("run")

    def fake_asyncio_run(awaitable: object) -> None:
        calls.append("asyncio.run")
        assert hasattr(awaitable, "close")
        awaitable.close()  # type: ignore[union-attr]

    monkeypatch.setattr(inbox_worker_main, "run", fake_run)
    monkeypatch.setattr(inbox_worker_main.asyncio, "run", fake_asyncio_run)

    inbox_worker_main.main()

    assert calls == ["asyncio.run"]
