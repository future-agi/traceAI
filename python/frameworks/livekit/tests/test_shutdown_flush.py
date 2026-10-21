"""
Tests for traceai-livekit: flushing the tracer provider when a LiveKit job shuts down.

A job process on Linux ends without running `atexit`, and LiveKit flushes only its own span
processors, so the spans that end last (the root `agent_session` span among them) stayed in the
batch processor and never reached Future AGI. `enable_*` now registers a shutdown callback on the
job that flushes the provider, and a flush when the process exits, for the spans LiveKit ends
after the shutdown callbacks.

Every test that needs a job context enters `fake_job_context()` inside the coroutine it runs:
building the context creates a LiveKit room, which needs a running event loop once any earlier
`asyncio.run` has left none set. The shutdown callbacks are run the way LiveKit runs them
(awaited with the shutdown reason). One test runs a real job process; nothing here reaches the
network: exporters are stubbed and every endpoint points at an unreachable local address.
"""

import asyncio
import logging
import multiprocessing
import multiprocessing.util
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, List

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
    SET_UP_AT_IMPORT,
    UNREACHABLE_URL,
    URL_VARIABLES,
    entrypoint_after_set_up_at_import,
    run_job_process,
)

LOGGER_NAME = "traceai_livekit.integration"


@pytest.fixture(autouse=True)
def _nothing_leaves_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in URL_VARIABLES:
        monkeypatch.setenv(name, UNREACHABLE_URL)


def _cancel_exit_finalizers() -> None:
    for finalizer in list(multiprocessing.util._finalizer_registry.values()):
        if getattr(finalizer, "_callback", None) is integration._run_exit_flush:
            finalizer.cancel()


@pytest.fixture(autouse=True)
def _a_fresh_exit_flush(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Each test starts as a new process would: nothing registered to flush at exit, including what
    # tests in other files registered by enabling outside this fixture.
    _cancel_exit_finalizers()
    monkeypatch.setattr(integration, "_exit_flush_providers", [])
    monkeypatch.setattr(integration, "_exit_flush_finalizer", None)
    monkeypatch.setattr(integration, "_exit_flush_pid", None)
    monkeypatch.setattr(integration, "_exit_flush_lock", threading.Lock())
    yield
    # And leaves nothing behind for the test process's own exit.
    _cancel_exit_finalizers()


def _exit_finalizers() -> int:
    # multiprocessing keeps its exit finalizers in a private registry; counting ours there is the
    # only way to see the registration without exiting the process.
    return sum(
        1
        for finalizer in list(multiprocessing.util._finalizer_registry.values())
        if getattr(finalizer, "_callback", None) is integration._run_exit_flush
    )


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


def test_enabling_outside_a_job_flushes_at_process_exit_without_a_warning(
    caplog: pytest.LogCaptureFixture, make_provider: Any
) -> None:
    provider = make_provider()
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert enable_http_attribute_mapping() is True

    assert _warnings(caplog) == []
    assert integration._exit_flush_providers == [provider]


def test_the_flush_waits_stay_inside_livekits_exit_budget() -> None:
    # LiveKit gives a job process `shutdown_process_timeout` to exit (10 seconds, from 1.3). Room
    # disconnect and LiveKit's own clean-up share it with both flushes; flushes that used it up
    # would get the process killed before that clean-up ran.
    from dataclasses import fields

    from livekit.agents.worker import ServerOptions

    (budget,) = [f.default for f in fields(ServerOptions) if f.name == "shutdown_process_timeout"]
    flushes = integration._SHUTDOWN_FLUSH_TIMEOUT_S + integration._EXIT_FLUSH_TIMEOUT_S
    assert flushes + 4 <= budget


def test_a_second_provider_in_one_job_is_flushed_too(make_provider: Any) -> None:
    first = make_provider()
    second = make_provider()

    async def job() -> int:
        with fake_job_context() as ctx:
            before = len(ctx._shutdown_callbacks)
            assert flush_on_job_shutdown(first) is True
            assert flush_on_job_shutdown(second) is True
            assert flush_on_job_shutdown(first) is True
            assert flush_on_job_shutdown(second) is True
            return len(ctx._shutdown_callbacks) - before

    assert asyncio.run(job()) == 2


def test_the_process_exit_flush_is_registered_once_per_process(make_provider: Any) -> None:
    first = make_provider()
    second = make_provider()
    before = _exit_finalizers()

    flush_on_job_shutdown(first)
    flush_on_job_shutdown(first)
    flush_on_job_shutdown(second)

    assert _exit_finalizers() - before == 1
    assert integration._exit_flush_providers == [first, second]


def test_the_process_exit_flush_is_registered_once_under_concurrent_set_up(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any
) -> None:
    # LiveKit's thread executor runs several jobs in one process, which can enable at once.
    providers = [make_provider() for _ in range(4)]
    real_finalize = multiprocessing.util.Finalize

    def slow_finalize(*args: Any, **kwargs: Any) -> Any:
        # Widens the window between the check and the registration, so a race shows every time.
        time.sleep(0.05)
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(multiprocessing.util, "Finalize", slow_finalize)
    start = threading.Barrier(len(providers))

    def set_up(provider: Any) -> None:
        start.wait()
        flush_on_job_shutdown(provider)

    before = _exit_finalizers()
    threads = [threading.Thread(target=set_up, args=(p,)) for p in providers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert _exit_finalizers() - before == 1
    assert len(integration._exit_flush_providers) == len(providers)


def test_a_child_process_gets_its_own_exit_flush_and_keeps_the_providers(
    make_provider: Any,
) -> None:
    # A finalizer runs only in the process that made it, so a forked child needs a new one; the
    # providers it inherited are copies its own spans go to, so it still flushes them.
    inherited, own = make_provider(), make_provider()
    flush_on_job_shutdown(inherited)
    integration._exit_flush_pid = -1
    before = _exit_finalizers()

    flush_on_job_shutdown(own)

    assert _exit_finalizers() - before == 1
    assert integration._exit_flush_providers == [inherited, own]


def test_the_exit_flush_is_made_again_after_a_child_clears_its_finalizers(
    make_provider: Any,
) -> None:
    provider = make_provider()
    flush_on_job_shutdown(provider)
    # What multiprocessing does in a fork or forkserver child just before its target runs: clear
    # the finalizers, then run the after-fork hooks. Ours must be among them.
    integration._exit_flush_finalizer.cancel()
    assert _exit_finalizers() == 0
    hooks = [
        (func, obj)
        for (_, _, func), obj in list(multiprocessing.util._afterfork_registry.items())
        if func is integration._arm_exit_flush_after_fork
    ]
    assert len(hooks) == 1

    for func, obj in hooks:
        func(obj)

    assert _exit_finalizers() == 1
    assert integration._exit_flush_providers == [provider]


def test_the_process_exit_flush_flushes_every_registered_provider(make_provider: Any) -> None:
    providers = [make_provider(), make_provider()]
    flushed: List[Any] = []
    for provider in providers:
        flush_on_job_shutdown(provider)
        provider.force_flush = (lambda p: lambda *a, **k: flushed.append(p) or True)(provider)

    integration._run_exit_flush()

    assert flushed == providers


def test_a_blocked_exit_flush_does_not_hold_up_the_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_provider: Any,
) -> None:
    provider = make_provider()
    monkeypatch.setattr(integration, "_EXIT_FLUSH_TIMEOUT_S", 0.2)
    release = threading.Event()
    flush_on_job_shutdown(provider)
    # Bounded, so the thread ends even when the bound under test is gone.
    monkeypatch.setattr(provider, "force_flush", lambda *a, **k: release.wait(timeout=3))
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    started = time.monotonic()
    try:
        integration._run_exit_flush()
    finally:
        elapsed = time.monotonic() - started
        release.set()

    assert elapsed < 2
    assert len(_warnings(caplog)) == 1


def test_a_blocked_provider_does_not_stop_the_others_flushing_at_exit(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any
) -> None:
    blocked, other = make_provider(), make_provider()
    monkeypatch.setattr(integration, "_EXIT_FLUSH_TIMEOUT_S", 0.5)
    release = threading.Event()
    flushed: List[Any] = []
    for provider in (blocked, other):
        flush_on_job_shutdown(provider)
    monkeypatch.setattr(blocked, "force_flush", lambda *a, **k: release.wait(timeout=3))
    monkeypatch.setattr(other, "force_flush", lambda *a, **k: flushed.append(other) or True)

    try:
        integration._run_exit_flush()
        # Read before the blocked flush is released, which would let a flush queued behind it run.
        flushed_in_time = list(flushed)
    finally:
        release.set()

    assert flushed_in_time == [other]


def test_a_failing_exit_flush_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_provider: Any,
) -> None:
    provider = make_provider()
    flush_on_job_shutdown(provider)

    def fail(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("collector at http://collector.invalid refused the connection")

    monkeypatch.setattr(provider, "force_flush", fail)
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    integration._run_exit_flush()

    (record,) = _warnings(caplog)
    assert "failed" in record.getMessage()
    assert "collector.invalid" not in record.getMessage()


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


def _the_pre_1_5_3_signature_outside_a_job() -> Any:
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
        _the_pre_1_5_3_signature_outside_a_job,
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


def test_the_pre_1_5_3_job_api_still_registers_during_a_job(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any
) -> None:
    # Before livekit-agents 1.5.3, get_job_context took no arguments; called with any, it raises
    # TypeError and the flush would never be registered.
    make_provider()

    async def job() -> List[Any]:
        with fake_job_context() as ctx:

            def get_job_context() -> Any:
                return ctx

            monkeypatch.setattr("livekit.agents.get_job_context", get_job_context)
            before = len(ctx._shutdown_callbacks)
            registered = flush_on_job_shutdown()
            return [registered, len(ctx._shutdown_callbacks) - before]

    assert asyncio.run(job()) == [True, 1]


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
    # LiveKit ends its job_shutdown span after the shutdown callbacks; only the process-exit flush
    # can send it. With set-up inside the entrypoint it is the job trace's root: LiveKit starts
    # job_entrypoint before tracing is on, so that span is never recorded.
    assert "job_shutdown" in names, f"spans the exporter received: {names}"


def test_set_up_at_import_is_flushed_when_a_real_job_process_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An agent module that sets tracing up at its top runs that set-up while the job process
    # imports it, before multiprocessing clears the exit finalizers the process started with.
    try:
        mp_ctx = multiprocessing.get_context("forkserver")
    except ValueError:
        pytest.skip("the forkserver start method is not available here")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", str(NEVER_MS))
    exported = tmp_path / "exported-span-names.txt"
    exported.write_text("")

    exited = asyncio.run(
        run_job_process(
            mp_ctx,
            str(exported),
            initialize_process_fnc=SET_UP_AT_IMPORT,
            job_entrypoint_fnc=entrypoint_after_set_up_at_import,
        )
    )

    assert exited, "the job process did not exit within the time allowed"
    names = exported.read_text().splitlines()
    # No job-shutdown flush is registered in this set-up: the process-exit flush sends them all,
    # and job_entrypoint, recorded because tracing was on before the job started, is the root.
    for name in ("agent_session", "job_shutdown", "job_entrypoint"):
        assert name in names, f"spans the exporter received: {names}"
