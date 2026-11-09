"""Real Task worker boundaries: context, privacy, native lifecycle and restoration."""

import asyncio
import json
import threading
from concurrent.futures import CancelledError, InvalidStateError
from contextvars import ContextVar
from importlib.metadata import version

import pytest
from crewai import Task
from opentelemetry import baggage, context
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import StatusCode
from packaging.version import Version
from wrapt import FunctionWrapper

import test_provider_composition as composition_tests

provider_composition = composition_tests.provider_composition


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async"])
def test_task_workers_honor_suppression(provider_composition, parallel, api):
    build, inst, llm_inst, provider, memory, calls, _ = provider_composition
    if llm_inst is None:
        pytest.skip("Install traceAI-openai to test provider composition")
    for item in (inst, llm_inst):
        item.instrument(tracer_provider=provider)
    with suppress_instrumentation():
        result = getattr(build(("gpt-4o-mini", "gpt-4o"), parallel=parallel), api)()
        if api.endswith("async"):
            result = asyncio.run(result)
        assert result.raw == "positive-output"
    assert len(calls) == 2
    assert not memory.get_finished_spans()
    # A suppressed call must not contaminate the next dispatch.
    result = build(("gpt-4o-mini", "gpt-4o"), parallel=True).kickoff()
    assert result.raw == "positive-output"
    assert len(memory.get_finished_spans()) == 5


def test_worker_context_coexists_with_native_wrapper_and_restores(
    provider_composition, monkeypatch
):
    build, inst, _, provider, memory, _, _ = provider_composition
    original = Task._execute_task_async
    execute_async = Task.execute_async
    thread_start = threading.Thread.start
    marker = ContextVar("worker-marker", default="empty")
    observed = []

    def native(wrapped, instance, args, kwargs):
        observed.append((marker.get(), baggage.get_baggage("dispatch")))
        with provider.get_tracer("native").start_as_current_span("native-worker"):
            return wrapped(*args, **kwargs)

    native_wrapper = FunctionWrapper(original, native)
    monkeypatch.setattr(Task, "_execute_task_async", native_wrapper)
    for cycle in range(2):
        inst.instrument(tracer_provider=provider)
        try:
            with provider.get_tracer("caller").start_as_current_span("caller") as caller:
                marker_token = marker.set(f"request-{cycle}")
                baggage_token = context.attach(baggage.set_baggage("dispatch", str(cycle)))
                try:
                    assert build(("gpt-4o-mini", "gpt-4o"), parallel=True).kickoff().raw == "positive-output"
                finally:
                    context.detach(baggage_token)
                    marker.reset(marker_token)
                spans = memory.get_finished_spans()
                crew = next(s for s in spans if s.name.startswith("Crew."))
                native_span = next(s for s in spans if s.name == "native-worker")
                tasks = [s for s in spans if s.name.startswith("Task.")]
                assert len(spans) == 4 and len(tasks) == 2
                assert crew.parent.span_id == caller.get_span_context().span_id
                assert native_span.parent.span_id == crew.context.span_id
                assert {s.parent.span_id for s in tasks} == {crew.context.span_id, native_span.context.span_id}
                assert marker.get() == "empty" and baggage.get_baggage("dispatch") is None
                assert all(s.end_time for s in spans)
        finally:
            inst.uninstrument()
        assert vars(Task)["_execute_task_async"] is native_wrapper
        assert Task.execute_async is execute_async
        assert threading.Thread.start is thread_start
        memory.clear()
    assert observed == [("request-0", "0"), ("request-1", "1")]


@pytest.mark.parametrize("outcome", ["error", "cancel"])
def test_worker_error_and_cancellation_preserve_native_behavior(
    provider_composition, monkeypatch, outcome
):
    build, inst, llm_inst, provider, memory, _, state = provider_composition
    if llm_inst is None:
        pytest.skip("Install traceAI-openai to test provider composition")
    native_outcome = None
    for instrumented in (False, True):
        memory.clear()
        state["entered"].clear()
        state["release"].clear()
        state["block"] = True
        state["fail"] = outcome == "error"
        thread_errors = []
        thread_failed = threading.Event()

        def on_error(args):
            thread_errors.append(type(args.exc_value))
            thread_failed.set()

        monkeypatch.setattr(threading, "excepthook", on_error)
        if instrumented:
            for item in (inst, llm_inst):
                item.instrument(tracer_provider=provider)
        # Direct public Task execution avoids Crew waiting forever on the
        # legacy upstream Future when its worker raises (native 0.41.1 bug).
        task = build().tasks[0]
        before_threads = set(threading.enumerate())
        with provider.get_tracer("caller").start_as_current_span("caller") as caller:
            future = task.execute_async()
            try:
                assert state["entered"].wait(10)
                assert not memory.get_finished_spans(), "Worker spans must remain open"
                if outcome == "cancel":
                    assert future.cancel()
                    with pytest.raises(CancelledError):
                        future.result(timeout=10)
            finally:
                state["release"].set()
                # Join actual native workers, not just the Future: cancellation
                # can finish the Future while HTTP and Task spans remain active.
                for thread in set(threading.enumerate()) - before_threads:
                    thread.join(timeout=10)
                    assert not thread.is_alive()
            if outcome == "error":
                if version("crewai") == "0.41.1":
                    assert not future.done() and thread_failed.is_set()
                else:
                    with pytest.raises(Exception):
                        future.result(timeout=10)
            else:
                assert future.cancelled()
                assert thread_errors == [InvalidStateError]
            signature = (future.done(), future.cancelled(), tuple(thread_errors))
            if not instrumented:
                native_outcome = signature
                assert not memory.get_finished_spans()
            else:
                assert signature == native_outcome
                spans = memory.get_finished_spans()
                tasks = [s for s in spans if s.name.startswith("Task.")]
                llms = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
                assert len(tasks) == len(llms) == 1 and len(spans) == 2
                assert tasks[0].parent.span_id == caller.get_span_context().span_id
                assert llms[0].parent.span_id == tasks[0].context.span_id
                status = StatusCode.ERROR if outcome == "error" else StatusCode.OK
                assert all(s.end_time and s.status.status_code is status for s in spans)
                assert "synthetic-provider-key" not in json.dumps([dict(s.attributes) for s in spans])


def test_modern_task_context_hook_is_not_replaced(provider_composition):
    _, inst, _, provider, _, _, _ = provider_composition
    original = Task._execute_task_async
    inst.instrument(tracer_provider=provider)
    if Version(version("crewai")) >= Version("1.15.0"):
        assert Task._execute_task_async is original
    inst.uninstrument()
    assert Task._execute_task_async is original
