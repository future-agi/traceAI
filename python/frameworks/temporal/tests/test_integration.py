import asyncio
import json
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import pytest
from fi_instrumentation import FITracer, TraceConfig, suppress_tracing
from fi_instrumentation.auto import wire
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from temporalio import activity, workflow
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import ActivityInboundInterceptor, Interceptor, Replayer, Worker

from traceai_temporal import TemporalInstrumentor, TemporalInterceptor
from .conftest import CACHE
from .workflow_interceptors import SuppressWorkflowInterceptor
from .workflows import (
    ChildWorkflow, ContinueWorkflow, FailedWorkflow, LocalWorkflow, ParentWorkflow,
    RetryWorkflow, SignalFailureWorkflow, TimeoutWorkflow, UpdateStartWorkflow,
    WaitingWorkflow, flaky_activity, llm_activity, sync_activity,
)

pytestmark = pytest.mark.integration
HEAVY = os.environ.get("TRACEAI_TEST_HEAVY_HELPER", "/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh")
RUNTIME_PREFIX = ["bash", HEAVY] if Path(HEAVY).is_file() else []


@pytest.fixture
async def environment():
    downloads = CACHE / "test-server-download"
    downloads.mkdir(parents=True, exist_ok=True)
    # Download failure is a real test failure, not a silently skipped integration.
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(downloads)) as env:
        yield env


@contextmanager
def mock_llm():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["messages"][0]["content"] == "ping"
            data = json.dumps({
                "id": "mock-response", "model": "test-model",
                "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def traced_client(env, provider):
    config = env.client.config()
    return Client(**{**config, "interceptors": [*config["interceptors"], TemporalInterceptor(provider)]})


def only(exporter, name):
    spans = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(spans) == 1, [(s.name, s.context.span_id) for s in exporter.get_finished_spans()]
    return spans[0]


def assert_tree(exporter):
    start = only(exporter, "temporal.start_workflow ParentWorkflow")
    parent = only(exporter, "temporal.workflow ParentWorkflow")
    child = only(exporter, "temporal.workflow ChildWorkflow")
    assert parent.parent.span_id == start.context.span_id
    assert child.parent.span_id == parent.context.span_id
    activities = [s for s in exporter.get_finished_spans() if s.name == "temporal.activity llm_activity"]
    models = [s for s in exporter.get_finished_spans() if s.name == "chat test-model"]
    assert len(activities) == len(models) == 2
    assert {a.parent.span_id for a in activities} == {parent.context.span_id, child.context.span_id}
    assert {s.parent.span_id for s in models} == {a.context.span_id for a in activities}
    assert {s.context.trace_id for s in exporter.get_finished_spans()} == {start.context.trace_id}
    assert parent.end_time - parent.start_time >= 2 * 60 * 60 * 1_000_000_000
    assert {s.attributes["gen_ai.span.kind"] for s in [start, parent, *activities, *models]} == {"CHAIN", "AGENT", "TOOL", "LLM"}
    for model in models:
        assert model.attributes["output.value"] == "pong"
        assert model.attributes["traceai.capture.layer"] == "wire"


async def test_real_time_skipping_tree_wire_and_replay(environment, tracing):
    provider, exporter = tracing
    client = await traced_client(environment, provider)
    wire.enable(FITracer(provider.get_tracer("wire"), config=TraceConfig()), transports=["httpx"])
    queue = str(uuid4())
    with mock_llm() as url:
        async with Worker(client, task_queue=queue, workflows=[ParentWorkflow, ChildWorkflow], activities=[llm_activity]):
            handle = await client.start_workflow(ParentWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue)
            assert await asyncio.wait_for(handle.result(), 40) == ["pong", "pong"]
            history = await handle.fetch_history()
    assert_tree(exporter)
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ParentWorkflow, ChildWorkflow], interceptors=[TemporalInterceptor(provider)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


async def test_local_sync_activity_retry_failure_and_continue(environment, tracing):
    provider, exporter = tracing
    client = await traced_client(environment, provider)
    queue = str(uuid4())
    with patch("opentelemetry.trace.get_tracer_provider", return_value=provider), ThreadPoolExecutor(max_workers=2) as executor:
        async with Worker(client, task_queue=queue,
                          workflows=[LocalWorkflow, RetryWorkflow, FailedWorkflow, ContinueWorkflow],
                          activities=[sync_activity, flaky_activity], activity_executor=executor):
            assert await client.execute_workflow(LocalWorkflow.run, id=str(uuid4()), task_queue=queue) == "local-result"
            assert await client.execute_workflow(RetryWorkflow.run, id=str(uuid4()), task_queue=queue) == "retried"
            with pytest.raises(WorkflowFailureError) as caught:
                await client.execute_workflow(FailedWorkflow.run, id=str(uuid4()), task_queue=queue)
            assert "sensitive workflow failure" in str(caught.value.cause)
            assert await client.execute_workflow(ContinueWorkflow.run, 1, id=str(uuid4()), task_queue=queue) == 0
    local = only(exporter, "temporal.activity sync_activity")
    assert local.attributes["temporal.activity.local"] is True
    assert only(exporter, "sync.inner").parent.span_id == local.context.span_id
    assert local.parent.span_id == only(exporter, "temporal.workflow LocalWorkflow").context.span_id
    attempts = [s for s in exporter.get_finished_spans() if s.name == "temporal.activity flaky_activity"]
    assert [s.attributes["temporal.activity.attempt"] for s in attempts] == [1, 2]
    assert [s.status.status_code for s in attempts] == [trace.StatusCode.ERROR, trace.StatusCode.OK]
    assert [json.loads(s.attributes["input.value"]) for s in attempts] == [[], []]
    assert "output.value" not in attempts[0].attributes
    assert json.loads(attempts[1].attributes["output.value"]) == "retried"
    assert all(not s.events and "private" not in (s.status.description or "") for s in attempts)
    failed = only(exporter, "temporal.workflow FailedWorkflow")
    assert failed.status.status_code is trace.StatusCode.ERROR
    assert json.loads(failed.attributes["input.value"]) == []
    assert "output.value" not in failed.attributes
    assert not failed.events
    assert "sensitive" not in (failed.status.description or "")
    continued = [s for s in exporter.get_finished_spans() if s.name == "temporal.workflow ContinueWorkflow"]
    assert len(continued) == 2
    assert continued[1].parent.span_id == continued[0].context.span_id


async def test_evicted_workflow_resumes_with_stable_parent_and_cancellation(environment, tracing):
    provider, exporter = tracing
    client = await traced_client(environment, provider)
    queue = str(uuid4())
    async with Worker(client, task_queue=queue, workflows=[WaitingWorkflow], max_cached_workflows=0):
        handle = await client.start_workflow(WaitingWorkflow.run, queue, id=str(uuid4()), task_queue=queue)
        # Query-free synchronization: wait until the workflow's first task completed.
        for _ in range(200):
            history = await handle.fetch_history()
            if any(e.HasField("workflow_task_completed_event_attributes") for e in history.events):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("workflow never reached its wait condition")
    assert not [s for s in exporter.get_finished_spans() if s.name == "temporal.workflow WaitingWorkflow"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with Worker(client, task_queue=queue, workflows=[WaitingWorkflow], activities=[sync_activity], activity_executor=executor):
            await handle.signal(WaitingWorkflow.finish)
            value = await asyncio.wait_for(handle.result(), 30)
            assert 0 <= float(value) < 1
            history = await handle.fetch_history()
            # Replaying without the interceptor proves it did not consume the
            # application's deterministic PRNG sequence or change activity args.
            await Replayer(workflows=[WaitingWorkflow]).replay_workflow(history)
            cancelled = await client.start_workflow(WaitingWorkflow.run, queue, id=str(uuid4()), task_queue=queue)
            await cancelled.cancel()
            with pytest.raises(WorkflowFailureError):
                await cancelled.result()
    finished = [s for s in exporter.get_finished_spans() if s.name == "temporal.workflow WaitingWorkflow"]
    assert len(finished) == 2
    activity_span = only(exporter, "temporal.activity sync_activity")
    assert activity_span.parent.span_id == finished[0].context.span_id
    assert finished[1].status.status_code is trace.StatusCode.ERROR


async def test_constructor_auto_instrumentation_and_suppression(environment, tracing):
    provider, exporter = tracing
    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    queue = str(uuid4())
    try:
        # Creating a client through its normal constructor picks up the interceptor.
        client = Client(**environment.client.config())
        async with Worker(client, task_queue=queue, workflows=[ContinueWorkflow]):
            with suppress_tracing():
                assert await client.execute_workflow(ContinueWorkflow.run, 0, id=str(uuid4()), task_queue=queue) == 0
            assert not exporter.get_finished_spans()
            assert await client.execute_workflow(ContinueWorkflow.run, 0, id=str(uuid4()), task_queue=queue) == 0
        assert len(exporter.get_finished_spans()) == 2
    finally:
        instrumentor.uninstrument()


async def test_headers_cross_activity_worker_process(environment, tracing, tmp_path):
    provider, exporter = tracing
    client = await traced_client(environment, provider)
    workflows_queue, activities_queue = str(uuid4()), str(uuid4())
    output, ready = tmp_path / "spans.jsonl", tmp_path / "ready"
    helper = Path(__file__).with_name("activity_worker.py")
    log_path = tmp_path / "worker.log"
    with log_path.open("w") as log:
        process = await asyncio.create_subprocess_exec(
            *RUNTIME_PREFIX, sys.executable, str(helper),
            environment.client.service_client.config.target_host,
            activities_queue, str(output), str(ready),
            stdout=log, stderr=log, start_new_session=True,
            env={**os.environ, "FI_AUTO_INSTRUMENT": "false"},
        )
        try:
            # The shared helper can legitimately queue behind another suite.
            # Budget that wait separately from actual workflow execution.
            for _ in range(6000):
                if ready.exists():
                    break
                if process.returncode is not None:
                    pytest.fail(log_path.read_text())
                await asyncio.sleep(0.1)
            else:
                pytest.fail("activity worker did not start; " + log_path.read_text())
            with mock_llm() as url:
                async with Worker(client, task_queue=workflows_queue, workflows=[ParentWorkflow]):
                    result = await client.execute_workflow(ParentWorkflow.run, {"url": url, "activities": activities_queue, "children": activities_queue}, id=str(uuid4()), task_queue=workflows_queue)
                    assert result == ["pong", "pong"]
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
    remote = [json.loads(line) for line in output.read_text().splitlines()]
    activities = [s for s in remote if s["name"] == "temporal.activity llm_activity"]
    models = [s for s in remote if s["name"] == "chat test-model"]
    assert len(activities) == len(models) == 2
    children = [s for s in remote if s["name"] == "temporal.workflow ChildWorkflow"]
    assert len(children) == 1
    parent_id = only(exporter, "temporal.workflow ParentWorkflow").context.span_id
    assert children[0]["parent"] == parent_id
    assert {s["parent"] for s in activities} == {parent_id, children[0]["span_id"]}
    assert {s["parent"] for s in models} == {s["span_id"] for s in activities}
    assert {s["trace_id"] for s in remote} == {only(exporter, "temporal.start_workflow ParentWorkflow").context.trace_id}


async def test_concurrent_workflows_do_not_cross_link(environment, tracing):
    provider, exporter = tracing
    client = await traced_client(environment, provider)
    wire.enable(FITracer(provider.get_tracer("wire"), config=TraceConfig()), transports=["httpx"])
    queue = str(uuid4())
    # V1's test-server clock is global; multiple result() unlocks otherwise race
    # and can time out unrelated activities. No timers need skipping in this case.
    with mock_llm() as url, environment.auto_time_skipping_disabled():
        async with Worker(client, task_queue=queue, workflows=[ChildWorkflow], activities=[llm_activity], max_concurrent_activities=2):
            results = await asyncio.gather(*[
                client.execute_workflow(ChildWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue)
                for _ in range(4)
            ])
    assert results == ["pong"] * 4
    spans = exporter.get_finished_spans()
    workflows = {s.attributes["temporal.workflow.id"]: s for s in spans if s.name == "temporal.workflow ChildWorkflow"}
    activities = [s for s in spans if s.name == "temporal.activity llm_activity"]
    models = [s for s in spans if s.name == "chat test-model"]
    assert len(workflows) == len(activities) == len(models) == 4
    for activity_span in activities:
        expected = workflows[activity_span.attributes["temporal.workflow.id"]]
        assert activity_span.parent.span_id == expected.context.span_id
        assert activity_span.context.trace_id == expected.context.trace_id
    assert {s.parent.span_id for s in models} == {s.context.span_id for s in activities}
    assert len({s.context.trace_id for s in workflows.values()}) == 4


async def test_uninstrumented_baseline_matches_results_retries_and_errors(environment, tracing):
    provider, _ = tracing

    async def run(client):
        queue = str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(client, task_queue=queue, workflows=[LocalWorkflow, RetryWorkflow, FailedWorkflow],
                              activities=[sync_activity, flaky_activity], activity_executor=executor):
                local = await client.execute_workflow(LocalWorkflow.run, id=str(uuid4()), task_queue=queue)
                retried = await client.execute_workflow(RetryWorkflow.run, id=str(uuid4()), task_queue=queue)
                with pytest.raises(WorkflowFailureError) as caught:
                    await client.execute_workflow(FailedWorkflow.run, id=str(uuid4()), task_queue=queue)
                error = caught.value.cause
                return local, retried, type(error), error.message, error.non_retryable

    baseline = await run(environment.client)
    instrumented = await run(await traced_client(environment, provider))
    assert baseline == instrumented


async def test_worker_only_root_and_full_wire_suppression(environment, tracing):
    provider, exporter = tracing
    wire.enable(FITracer(provider.get_tracer("wire"), config=TraceConfig()), transports=["httpx"])
    queue = str(uuid4())
    with mock_llm() as url:
        async with Worker(environment.client, task_queue=queue, workflows=[ChildWorkflow], activities=[llm_activity],
                          interceptors=[TemporalInterceptor(provider)]):
            assert await environment.client.execute_workflow(ChildWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue) == "pong"
    root = only(exporter, "temporal.workflow ChildWorkflow")
    activity_span = only(exporter, "temporal.activity llm_activity")
    assert root.parent is None
    assert root.context.trace_id == activity_span.context.trace_id
    assert activity_span.parent.span_id == root.context.span_id
    exporter.clear()
    client = await traced_client(environment, provider)
    with mock_llm() as url:
        async with Worker(client, task_queue=queue, workflows=[ChildWorkflow], activities=[llm_activity]):
            with suppress_tracing():
                assert await client.execute_workflow(ChildWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue) == "pong"
    assert not exporter.get_finished_spans()


async def test_native_workflow_timeout_matches_baseline_and_exports_failure(environment, tracing):
    if not hasattr(workflow, "is_failure_exception"):
        pytest.skip("older SDKs treat Python TimeoutError as a task failure, not a terminal workflow failure")
    provider, exporter = tracing
    outcomes = []
    for client in (environment.client, await traced_client(environment, provider)):
        queue = str(uuid4())
        async with Worker(client, task_queue=queue, workflows=[TimeoutWorkflow]):
            handle = await client.start_workflow(TimeoutWorkflow.run, id=str(uuid4()), task_queue=queue)
            with pytest.raises(WorkflowFailureError) as caught:
                await asyncio.wait_for(handle.result(), 30)
            outcomes.append((type(caught.value.cause), caught.value.cause.type, (await handle.describe()).status))
            history = await handle.fetch_history()
    assert outcomes[0] == outcomes[1]
    assert outcomes[1][1] == "TimeoutError"
    completed = only(exporter, "temporal.workflow TimeoutWorkflow")
    assert completed.status.status_code is trace.StatusCode.ERROR
    assert completed.attributes["error.type"] == "TimeoutError"
    assert completed.parent.span_id == only(exporter, "temporal.start_workflow TimeoutWorkflow").context.span_id
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[TimeoutWorkflow], interceptors=[TemporalInterceptor(provider)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


async def test_signal_failure_completes_activity_parent_once_and_matches_baseline(environment, tracing):
    provider, exporter = tracing
    outcomes = []
    with mock_llm() as url:
        for client in (environment.client, await traced_client(environment, provider)):
            queue = str(uuid4())
            async with Worker(client, task_queue=queue, workflows=[SignalFailureWorkflow], activities=[llm_activity]):
                handle = await client.start_workflow(SignalFailureWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue)
                for _ in range(200):
                    if await handle.query(SignalFailureWorkflow.is_ready):
                        break
                    await asyncio.sleep(0.02)
                else:
                    pytest.fail("signal workflow did not finish its activity")
                await handle.signal(SignalFailureWorkflow.fail)
                with pytest.raises(WorkflowFailureError) as caught:
                    await asyncio.wait_for(handle.result(), 30)
                error = caught.value.cause
                outcomes.append((type(error), error.type, error.message, error.non_retryable, (await handle.describe()).status))
                history = await handle.fetch_history()
    assert outcomes[0] == outcomes[1]
    completed = only(exporter, "temporal.workflow SignalFailureWorkflow")
    assert completed.status.status_code is trace.StatusCode.ERROR
    assert completed.attributes["error.type"] == "ApplicationError"
    assert "private" not in (completed.status.description or "")
    assert completed.parent.span_id == only(exporter, "temporal.start_workflow SignalFailureWorkflow").context.span_id
    assert only(exporter, "temporal.activity llm_activity").parent.span_id == completed.context.span_id
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[SignalFailureWorkflow], interceptors=[TemporalInterceptor(provider)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


@pytest.mark.parametrize("suppressed", [False, True])
async def test_update_with_start_propagates_parent_and_suppression(environment, tracing, suppressed):
    provider, exporter = tracing
    client = await traced_client(environment, provider)
    wire.enable(FITracer(provider.get_tracer("wire"), config=TraceConfig()), transports=["httpx"])
    queue = str(uuid4())
    with mock_llm() as url:
        async with Worker(client, task_queue=queue, workflows=[UpdateStartWorkflow], activities=[llm_activity]):
            operation = WithStartWorkflowOperation(
                UpdateStartWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
            with provider.get_tracer("test").start_as_current_span("origin") as origin:
                with suppress_tracing() if suppressed else nullcontext():
                    result = await client.execute_update_with_start_workflow(UpdateStartWorkflow.finish, start_workflow_operation=operation)
            assert result == "updated"
            handle = await operation.workflow_handle()
            assert await asyncio.wait_for(handle.result(), 30) == "pong"
            history = await handle.fetch_history()
    headers = history.events[0].workflow_execution_started_event_attributes.header.fields
    from traceai_temporal._propagation import decode
    if suppressed:
        assert decode(headers) == {"disabled": True}
        assert [s.name for s in exporter.get_finished_spans()] == ["origin"]
    else:
        start = only(exporter, "temporal.start_workflow UpdateStartWorkflow")
        completed = only(exporter, "temporal.workflow UpdateStartWorkflow")
        activity_span = only(exporter, "temporal.activity llm_activity")
        assert start.parent.span_id == origin.get_span_context().span_id
        assert completed.parent.span_id == start.context.span_id
        assert activity_span.parent.span_id == completed.context.span_id
        assert only(exporter, "chat test-model").parent.span_id == activity_span.context.span_id
        assert {s.context.trace_id for s in exporter.get_finished_spans()} == {origin.get_span_context().trace_id}


async def test_outer_activity_suppression_preserves_wire_privacy_and_baseline(environment, tracing):
    provider, exporter = tracing
    observed = []

    class SuppressedActivity(ActivityInboundInterceptor):
        async def execute_activity(self, input):
            with suppress_tracing():
                return await super().execute_activity(input)

    class ObserveActivity(ActivityInboundInterceptor):
        async def execute_activity(self, input):
            observed.append(bool(context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)))
            return await super().execute_activity(input)

    class Outer(Interceptor):
        def intercept_activity(self, next):
            return SuppressedActivity(next)

    class Inner(Interceptor):
        def intercept_activity(self, next):
            return ObserveActivity(next)

    wire.enable(FITracer(provider.get_tracer("wire"), config=TraceConfig()), transports=["httpx"])
    with mock_llm() as url:
        for instrumented in (False, True):
            queue = str(uuid4())
            interceptors = [Outer(), *([TemporalInterceptor(provider)] if instrumented else []), Inner()]
            async with Worker(environment.client, task_queue=queue, workflows=[ChildWorkflow], activities=[llm_activity], interceptors=interceptors):
                assert await environment.client.execute_workflow(ChildWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue) == "pong"
    assert observed == [True, True]
    assert not [s for s in exporter.get_finished_spans() if s.name in ("temporal.activity llm_activity", "chat test-model")]
    only(exporter, "temporal.workflow ChildWorkflow")


@pytest.mark.parametrize("order", ["tracing_outer", "tracing_inner", "auto_worker"])
async def test_outer_workflow_suppression_preserves_activity_and_wire_privacy(environment, tracing, order):
    provider, exporter = tracing
    observed = []

    @activity.defn(name="llm_activity")
    async def probe(url: str) -> str:
        observed.append(bool(context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)))
        return await llm_activity(url)

    tracing_interceptor = TemporalInterceptor(provider)
    suppressor = SuppressWorkflowInterceptor()
    interceptors = {
        "tracing_outer": [tracing_interceptor, suppressor],
        "tracing_inner": [suppressor, tracing_interceptor],
        "auto_worker": [suppressor],
    }[order]
    # The uninstrumented client predates instrumentation. The worker constructor
    # hook must append tracing inside the application's workflow suppressor.
    instrumentor = TemporalInstrumentor()
    if order == "auto_worker":
        instrumentor.instrument(tracer_provider=provider)
    wire.enable(FITracer(provider.get_tracer("wire"), config=TraceConfig()), transports=["httpx"])
    queue = str(uuid4())
    try:
        with mock_llm() as url:
            async with Worker(environment.client, task_queue=queue, workflows=[ParentWorkflow, ChildWorkflow],
                              activities=[probe], interceptors=interceptors):
                handle = await environment.client.start_workflow(
                    ParentWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue,
                )
                assert await asyncio.wait_for(handle.result(), 40) == ["pong", "pong"]
                history = await handle.fetch_history()
    finally:
        if order == "auto_worker":
            instrumentor.uninstrument()
    assert observed == [True, True]
    assert not context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)
    spans = exporter.get_finished_spans()
    assert not [s for s in spans if s.attributes.get("gen_ai.span.kind") in ("TOOL", "LLM")]
    assert not [s for s in spans if s.attributes.get("output.value") == "pong"]
    if order != "tracing_outer":
        assert not spans
    from traceai_temporal._propagation import decode
    scheduled = [
        e.activity_task_scheduled_event_attributes.header.fields for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    children = [
        e.start_child_workflow_execution_initiated_event_attributes.header.fields for e in history.events
        if e.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert len(scheduled) == len(children) == 1
    assert all(decode(headers) == {"disabled": True} for headers in (*scheduled, *children))
    before = len(spans)
    await Replayer(workflows=[ParentWorkflow, ChildWorkflow], interceptors=interceptors).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


@pytest.mark.parametrize("instrument_before_client", [False, True])
@pytest.mark.parametrize("prepend", [False, True])
async def test_plugin_active_client_interceptor_inherited_without_duplicates(environment, tracing, instrument_before_client, prepend):
    # Plugins and active_config were introduced after our minimum supported SDK.
    import temporalio.client as sdk_client
    if not hasattr(sdk_client, "Plugin"):
        pytest.skip("client plugins require a newer Temporal SDK")
    provider, exporter = tracing
    explicit = TemporalInterceptor(provider)

    calls = []

    class Plugin(sdk_client.Plugin):
        def configure_client(self, config):
            calls.append("configure")
            existing = list(config["interceptors"])
            return {**config, "interceptors": [explicit, *existing] if prepend else [*existing, explicit]}

        async def connect_service_client(self, config, next):
            return await next(config)

    instrumentor = TemporalInstrumentor()
    if instrument_before_client:
        instrumentor.instrument(tracer_provider=provider)
    queue = str(uuid4())
    try:
        # Exercise both startup orders. A plugin configures the client after its
        # constructor hook, so inspecting constructor arguments alone is too early.
        client = Client(**{**environment.client.config(), "plugins": [Plugin()]})
        assert calls == ["configure"]
        active = client.config(active_config=True)["interceptors"]
        assert [i for i in active if isinstance(i, TemporalInterceptor)] == [explicit]
        if not instrument_before_client:
            assert not any(isinstance(i, TemporalInterceptor) for i in client.config()["interceptors"])
            instrumentor.instrument(tracer_provider=provider)
        with mock_llm() as url:
            async with Worker(client, task_queue=queue, workflows=[ChildWorkflow], activities=[llm_activity]):
                assert await client.execute_workflow(ChildWorkflow.run, {"url": url, "activities": queue}, id=str(uuid4()), task_queue=queue) == "pong"
    finally:
        instrumentor.uninstrument()
    start = only(exporter, "temporal.start_workflow ChildWorkflow")
    completed = only(exporter, "temporal.workflow ChildWorkflow")
    activity_span = only(exporter, "temporal.activity llm_activity")
    assert completed.parent.span_id == start.context.span_id
    assert activity_span.parent.span_id == completed.context.span_id
    assert len(exporter.get_finished_spans()) == 3
