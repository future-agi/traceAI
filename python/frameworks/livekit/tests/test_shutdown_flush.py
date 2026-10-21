"""
Tests for traceai-livekit: flushing the tracer provider when a LiveKit job shuts down.

A job process on Linux ends without running `atexit`, and LiveKit flushes only its own span
processors, so the spans that end last (the root `agent_session` span among them) stayed in the
batch processor and never reached Future AGI. `enable_*` now registers a shutdown callback on the
job that flushes the provider.

Every test that needs a job context enters `fake_job_context()` inside the coroutine it runs:
building the context creates a LiveKit room, which needs a running event loop once any earlier
`asyncio.run` has left none set. The shutdown callbacks are run the way LiveKit runs them
(awaited with the shutdown reason). One test runs a real job process; nothing here reaches the
network: exporters are stubbed and every endpoint points at an unreachable local address.
"""

import asyncio
import logging
import multiprocessing
import threading
import time
from pathlib import Path
from typing import Any, Callable, List

import pytest

pytest.importorskip("livekit.agents", minversion="1.8")

import traceai_livekit.integration as integration  # noqa: E402
from livekit.agents.testing import fake_job_context  # noqa: E402

from traceai_livekit import (  # noqa: E402
    enable_http_attribute_mapping,
    flush_on_job_shutdown,
)

from ._job_process_entrypoint import (  # noqa: E402
    NEVER_MS,
    UNREACHABLE_URL,
    URL_VARIABLES,
    run_job_process,
)

LOGGER_NAME = "traceai_livekit.integration"


@pytest.fixture(autouse=True)
def _nothing_leaves_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in URL_VARIABLES:
        monkeypatch.setenv(name, UNREACHABLE_URL)


async def _run_shutdown_callbacks(ctx: Any) -> None:
    # The job's callbacks live in a private list; reading it is the only way to run them as
    # LiveKit does without starting a worker.
    for callback in list(ctx._shutdown_callbacks):
        await callback("call ended")


def _warnings(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.WARNING
    ]


def test_a_span_ended_just_before_shutdown_is_exported_by_the_shutdown_flush(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    provider = make_provider(schedule_delay_millis=NEVER_MS)

    async def job() -> List[str]:
        with fake_job_context() as ctx:
            assert enable_http_attribute_mapping() is True
            provider.get_tracer("t").start_span("agent_session").end()
            # The batch has not been sent: only the shutdown flush can send it.
            before = [s.name for s in exported_spans]
            await _run_shutdown_callbacks(ctx)
            return before

    before = asyncio.run(job())

    assert before == []
    assert [s.name for s in exported_spans] == ["agent_session"]


def test_enabling_registers_exactly_one_shutdown_callback(
    make_provider: Any,
) -> None:
    make_provider()

    async def job() -> int:
        with fake_job_context() as ctx:
            before = len(ctx._shutdown_callbacks)
            enable_http_attribute_mapping()
            enable_http_attribute_mapping()
            assert flush_on_job_shutdown() is True
            return len(ctx._shutdown_callbacks) - before

    assert asyncio.run(job()) == 1


def test_the_shutdown_flush_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any
) -> None:
    provider = make_provider()
    flush_threads: List[int] = []
    daemon_flags: List[bool] = []

    def record_thread(*args: Any, **kwargs: Any) -> bool:
        flush_threads.append(threading.get_ident())
        # A daemon thread, so a flush still running cannot keep the process from exiting.
        daemon_flags.append(threading.current_thread().daemon)
        return True

    async def job() -> int:
        with fake_job_context() as ctx:
            enable_http_attribute_mapping()
            monkeypatch.setattr(provider, "force_flush", record_thread)
            await _run_shutdown_callbacks(ctx)
            return threading.get_ident()

    loop_thread = asyncio.run(job())

    assert len(flush_threads) == 1
    assert flush_threads[0] != loop_thread
    assert daemon_flags == [True]


def test_a_blocked_flush_does_not_hold_up_job_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_provider: Any,
) -> None:
    provider = make_provider()
    monkeypatch.setattr(integration, "_SHUTDOWN_FLUSH_TIMEOUT_S", 0.2)
    release = threading.Event()

    def block_until_released(*args: Any, **kwargs: Any) -> bool:
        # Bounded, so a flush wrongly run on the event loop fails the timing check below instead
        # of freezing the loop, and the test, for good.
        release.wait(timeout=3)
        return True

    async def job() -> None:
        with fake_job_context() as ctx:
            enable_http_attribute_mapping()
            monkeypatch.setattr(provider, "force_flush", block_until_released)
            caplog.clear()
            # The test's own bound: far longer than the patched wait, so only a flush that is
            # waited on without a limit gets caught by it.
            await asyncio.wait_for(_run_shutdown_callbacks(ctx), 5)

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    # Timed around the whole run, loop shutdown included: closing the loop joins its default
    # executor, as LiveKit does before the job process exits, so a flush run there would show here.
    started = time.monotonic()
    try:
        asyncio.run(job())
    finally:
        elapsed = time.monotonic() - started
        release.set()

    assert elapsed < 2
    assert len(_warnings(caplog)) == 1


def test_a_failing_flush_does_not_break_job_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_provider: Any,
) -> None:
    provider = make_provider()

    def fail(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("collector at http://collector.invalid refused the connection")

    async def job() -> None:
        with fake_job_context() as ctx:
            enable_http_attribute_mapping()
            monkeypatch.setattr(provider, "force_flush", fail)
            caplog.clear()
            # Well under the real wait: a failure that is not carried back would only show
            # up as that wait running out.
            await asyncio.wait_for(_run_shutdown_callbacks(ctx), 5)

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    asyncio.run(job())

    (record,) = _warnings(caplog)
    assert "failed" in record.getMessage()
    assert "collector.invalid" not in record.getMessage()


def test_enabling_outside_a_job_registers_nothing_and_says_how_to_flush(
    caplog: pytest.LogCaptureFixture, make_provider: Any
) -> None:
    make_provider()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert enable_http_attribute_mapping() is True

    (record,) = [r for r in _warnings(caplog) if "flush_on_job_shutdown" in r.getMessage()]
    assert record.name == LOGGER_NAME


def test_flush_on_job_shutdown_registers_only_inside_a_job(
    make_provider: Any,
) -> None:
    make_provider()

    async def job() -> List[Any]:
        with fake_job_context() as ctx:
            before = len(ctx._shutdown_callbacks)
            inside = flush_on_job_shutdown()
            added = len(ctx._shutdown_callbacks) - before
        return [inside, added, flush_on_job_shutdown()]

    inside, added, after_the_job = asyncio.run(job())

    assert inside is True
    assert added == 1
    assert after_the_job is False


def _the_pre_1_6_signature_outside_a_job() -> Any:
    raise RuntimeError("no job context found")


def _an_unexpected_signature(*args: Any, **kwargs: Any) -> Any:
    raise TypeError("get_job_context() got an unexpected keyword argument")


def _a_missing_job_api() -> Any:
    raise ImportError("no job api")


def _a_job_without_shutdown_callbacks() -> Any:
    return object()


@pytest.mark.parametrize(
    "get_job_context",
    [
        _the_pre_1_6_signature_outside_a_job,
        _an_unexpected_signature,
        _a_missing_job_api,
        _a_job_without_shutdown_callbacks,
    ],
)
def test_an_older_livekit_job_api_neither_raises_nor_registers(
    monkeypatch: pytest.MonkeyPatch,
    make_provider: Any,
    get_job_context: Callable[[], Any],
) -> None:
    make_provider()
    # The real attribute, so the guard in `flush_on_job_shutdown` is what is under test.
    monkeypatch.setattr("livekit.agents.get_job_context", get_job_context)

    assert enable_http_attribute_mapping() is True
    assert flush_on_job_shutdown() is False


def test_agent_session_reaches_the_exporter_when_a_real_job_process_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The forkserver start method is what LiveKit uses on Linux; its processes end without
    # running `atexit`, which is how the last spans were lost.
    try:
        mp_ctx = multiprocessing.get_context("forkserver")
    except ValueError:
        pytest.skip("the forkserver start method is not available here")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", str(NEVER_MS))
    exported = tmp_path / "exported-span-names.txt"
    exported.write_text("")

    exited = asyncio.run(run_job_process(mp_ctx, str(exported)))

    assert exited, "the job process did not exit within the time allowed"
    names = exported.read_text().splitlines()
    assert "agent_session" in names, f"spans the exporter received: {names}"
