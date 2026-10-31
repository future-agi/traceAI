import asyncio
import base64
import json
from uuid import uuid4

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer, Worker

from traceai_temporal import TemporalInstrumentor, TemporalInterceptor
from traceai_temporal._content import MAX_BYTES
from .content_workflows import (
    ContentWorkflow, DeterministicContentWorkflow, UnserializableResultWorkflow, echo_content,
)
from .test_integration import environment, only  # Reuse the real loopback test server.

pytestmark = pytest.mark.integration


def configured_client(environment, provider, config):
    original = environment.client.config()
    return Client(**{**original, "interceptors": [*original["interceptors"], TemporalInterceptor(provider, config)]})


@pytest.mark.parametrize("mode", ["default", "hide_inputs", "hide_outputs", "env_inputs", "env_outputs"])
async def test_real_workflow_activity_default_capture_and_hide_flags(environment, tracing, monkeypatch, mode):
    provider, exporter = tracing
    if mode.startswith("env_"):
        monkeypatch.setenv("FI_HIDE_" + mode[4:].upper(), "true")
    config = TraceConfig(**({mode: True} if mode.startswith("hide_") else {}))
    client = configured_client(environment, provider, config)
    queue = str(uuid4())
    value = {"message": "private-content", "count": 3}
    async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[echo_content]):
        handle = await client.start_workflow(ContentWorkflow.run, value, id=str(uuid4()), task_queue=queue)
        assert await handle.result() == value
        history = await handle.fetch_history()
    for name in ("temporal.start_workflow ContentWorkflow", "temporal.workflow ContentWorkflow", "temporal.activity echo_content"):
        span = only(exporter, name)
        assert span.status.status_code is trace.StatusCode.OK
        for side in ("input",) if name.startswith("temporal.start_") else ("input", "output"):
            attrs = span.attributes
            if getattr(config, "hide_" + side + "s"):
                assert "private-content" not in attrs.get(side + ".value", "")
                assert side + ".mime_type" not in attrs
            else:
                assert json.loads(attrs[side + ".value"]) == ([value] if side == "input" else value)
                assert attrs[side + ".mime_type"] == "application/json"
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ContentWorkflow], interceptors=[TemporalInterceptor(provider, config)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


@pytest.mark.parametrize("initial_plugin", [False, True])
async def test_real_cloned_client_privacy_plugin_hides_content_without_duplicates(environment, tracing, initial_plugin):
    import temporalio.client as sdk_client
    if not hasattr(sdk_client, "Plugin"):
        pytest.skip("client plugins require a newer Temporal SDK")
    provider, exporter = tracing
    hidden = TemporalInterceptor(provider, TraceConfig(hide_inputs=True, hide_outputs=True))
    calls = []

    class Plugin(sdk_client.Plugin):
        def configure_client(self, config):
            calls.append("original")
            return config

        async def connect_service_client(self, config, next):
            return await next(config)

    class PrivacyPlugin(Plugin):
        def configure_client(self, config):
            calls.append("privacy")
            return {**config, "interceptors": [*config["interceptors"], hidden]}

    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        original = Client(**{**environment.client.config(), "plugins": [Plugin()] if initial_plugin else []})
        config = original.config()
        client = Client(**{**config, "plugins": [*config["plugins"], PrivacyPlugin()]})
        assert calls == (["original", "original", "privacy"] if initial_plugin else ["privacy"])
        queue = str(uuid4())
        value = {"message": "private-clone-content"}
        async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[echo_content]):
            handle = await client.start_workflow(ContentWorkflow.run, value, id=str(uuid4()), task_queue=queue)
            assert await handle.result() == value
            history = await handle.fetch_history()
    finally:
        instrumentor.uninstrument()
    assert len(exporter.get_finished_spans()) == 3
    for name in ("temporal.start_workflow ContentWorkflow", "temporal.workflow ContentWorkflow", "temporal.activity echo_content"):
        span = only(exporter, name)
        assert "private-clone-content" not in str(span.attributes)
        assert span.status.status_code is trace.StatusCode.OK
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ContentWorkflow], interceptors=[hidden]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


@pytest.mark.parametrize("inherited_automatic", [False, True])
async def test_real_worker_plugin_explicit_privacy_wins_after_constructor(environment, tracing, inherited_automatic):
    import temporalio.worker as sdk_worker
    if not hasattr(sdk_worker, "Plugin"):
        pytest.skip("worker plugins require a newer Temporal SDK")
    provider, exporter = tracing
    hidden = TemporalInterceptor(provider, TraceConfig(hide_inputs=True, hide_outputs=True))
    calls = []

    class PrivacyPlugin(sdk_worker.Plugin):
        def configure_worker(self, config):
            calls.append("configure")
            return {**config, "interceptors": [*config["interceptors"], hidden]}

        async def run_worker(self, worker, next):
            return await next(worker)

        def configure_replayer(self, config):
            return config

        def run_replayer(self, replayer, histories, next):
            return next(replayer, histories)

    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = Client(**environment.client.config()) if inherited_automatic else environment.client
        queue = str(uuid4())
        value = {"message": "private-worker-content"}
        plugins = [PrivacyPlugin()]
        async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[echo_content], plugins=plugins):
            handle = await client.start_workflow(ContentWorkflow.run, value, id=str(uuid4()), task_queue=queue)
            assert await handle.result() == value
            history = await handle.fetch_history()
        assert calls == ["configure"]
        assert len(plugins) == 1
    finally:
        instrumentor.uninstrument()
    for name in ("temporal.workflow ContentWorkflow", "temporal.activity echo_content"):
        span = only(exporter, name)
        assert "private-worker-content" not in str(span.attributes)
        assert span.status.status_code is trace.StatusCode.OK
    assert len(exporter.get_finished_spans()) == (3 if inherited_automatic else 2)
    # Worker privacy does not reconfigure an independently instrumented client.
    if inherited_automatic:
        assert "private-worker-content" in only(exporter, "temporal.start_workflow ContentWorkflow").attributes["input.value"]
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ContentWorkflow], interceptors=[hidden]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


def structural_media(shape):
    # A tiny synthetic WAV header/body is raw audio even below 256 base64 chars.
    raw = base64.b64encode(b"RIFF" + bytes([36, 0, 0, 0]) + b"WAVEfmt " + bytes(28) + b"data" + bytes(8)).decode("ascii")
    return raw, {
        "indexed_audio": {"input.audio.0": raw},
        "flattened_inline": {"inlineData.data": raw, "inlineData.mimeType": "audio/wav"},
        "nested_inline": {"inlineData": {"data": raw, "mimeType": "audio/wav"}},
    }[shape]


@pytest.mark.parametrize("shape", ["indexed_audio", "flattened_inline", "nested_inline"])
async def test_real_structural_media_is_removed_by_final_s1_sanitizer(environment, tracing, shape):
    raw, value = structural_media(shape)
    provider, exporter = tracing
    client = configured_client(environment, provider, TraceConfig())
    queue = str(uuid4())
    async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[echo_content]):
        handle = await client.start_workflow(ContentWorkflow.run, value, id=str(uuid4()), task_queue=queue)
        assert await handle.result() == value
        history = await handle.fetch_history()
    leaks = []
    for name in ("temporal.start_workflow ContentWorkflow", "temporal.workflow ContentWorkflow", "temporal.activity echo_content"):
        span = only(exporter, name)
        for side in ("input",) if name.startswith("temporal.start_") else ("input", "output"):
            if raw in span.attributes[side + ".value"]:
                leaks.append((name, side))
    assert len(exporter.get_finished_spans()) == 3
    # Check replay even while a shared-core dependency is broken, and report all
    # affected span families instead of stopping at the first client-side leak.
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ContentWorkflow], interceptors=[TemporalInterceptor(provider)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before
    assert not leaks, "\n".join(name + ": " + side + ".value" for name, side in leaks)


@pytest.mark.parametrize("shape", ["indexed_audio", "flattened_inline", "nested_inline"])
@pytest.mark.parametrize("source", ["config", "environment"])
async def test_real_structural_media_hide_controls_prevent_content_export(environment, tracing, monkeypatch, shape, source):
    raw, value = structural_media(shape)
    provider, exporter = tracing
    if source == "environment":
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
        monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
        config = TraceConfig()
    else:
        config = TraceConfig(hide_inputs=True, hide_outputs=True)
    client = configured_client(environment, provider, config)
    queue = str(uuid4())
    async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[echo_content]):
        handle = await client.start_workflow(ContentWorkflow.run, value, id=str(uuid4()), task_queue=queue)
        assert await handle.result() == value
        history = await handle.fetch_history()
    for name in ("temporal.start_workflow ContentWorkflow", "temporal.workflow ContentWorkflow", "temporal.activity echo_content"):
        span = only(exporter, name)
        assert span.status.status_code is trace.StatusCode.OK
        for side in ("input",) if name.startswith("temporal.start_") else ("input", "output"):
            assert raw not in span.attributes[side + ".value"]
            assert side + ".mime_type" not in span.attributes
    assert len(exporter.get_finished_spans()) == 3
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ContentWorkflow], interceptors=[TemporalInterceptor(provider, config)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


async def test_async_activity_handoff_preserves_result_and_is_not_failure(environment, tracing):
    provider, exporter = tracing
    outcomes = []
    for client in (environment.client, configured_client(environment, provider, TraceConfig())):
        started = asyncio.Event()
        tokens = []

        @activity.defn(name="echo_content")
        async def complete_async(value):
            tokens.append(activity.info().task_token)
            started.set()
            activity.raise_complete_async()

        queue = str(uuid4())
        async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[complete_async]):
            handle = await client.start_workflow(ContentWorkflow.run, "handoff-input", id=str(uuid4()), task_queue=queue)
            await asyncio.wait_for(started.wait(), 20)
            await client.get_async_activity_handle(task_token=tokens[0]).complete("completed-async")
            outcomes.append(await handle.result())
            history = await handle.fetch_history()
    assert outcomes == ["completed-async", "completed-async"]
    tool = only(exporter, "temporal.activity echo_content")
    assert tool.status.status_code is trace.StatusCode.OK
    assert tool.attributes["temporal.activity.async_completion"] is True
    assert "error.type" not in tool.attributes and not tool.events
    assert "output.value" not in tool.attributes  # The eventual remote result is not in this interceptor.
    assert json.loads(tool.attributes["input.value"]) == ["handoff-input"]
    assert json.loads(only(exporter, "temporal.workflow ContentWorkflow").attributes["output.value"]) == "completed-async"
    before = len(exporter.get_finished_spans())
    await Replayer(workflows=[ContentWorkflow], interceptors=[TemporalInterceptor(provider)]).replay_workflow(history)
    assert len(exporter.get_finished_spans()) == before


@pytest.mark.parametrize("value", [b"binary payload that must not be exported", "sensitive-prefix" + "x" * 700_000])
async def test_real_binary_and_oversized_payloads_preserve_results(environment, tracing, value):
    provider, exporter = tracing
    client = configured_client(environment, provider, TraceConfig())
    queue = str(uuid4())
    async with Worker(client, task_queue=queue, workflows=[ContentWorkflow], activities=[echo_content]):
        assert await client.execute_workflow(ContentWorkflow.run, value, id=str(uuid4()), task_queue=queue) == value
    for span in exporter.get_finished_spans():
        for key, content in span.attributes.items():
            if key in ("input.value", "output.value"):
                assert len(content.encode()) <= MAX_BYTES
                assert "sensitive-prefix" not in content
                assert "binary payload" not in content
                assert ("bytes" if type(value) is bytes else "content limit") in content


async def test_result_converter_failure_preserves_running_state_and_safe_content(environment, tracing):
    # Regression-check the prior verifier's documented post-interceptor result
    # conversion limitation. Instrumentation must not change the app's failure
    # into a successful completion, or stringify the unsupported result.
    provider, exporter = tracing
    for client in (environment.client, configured_client(environment, provider, TraceConfig())):
        queue = str(uuid4())
        async with Worker(client, task_queue=queue, workflows=[UnserializableResultWorkflow]):
            handle = await client.start_workflow(UnserializableResultWorkflow.run, id=str(uuid4()), task_queue=queue)
            try:
                for _ in range(200):
                    history = await handle.fetch_history()
                    if any(event.HasField("workflow_task_failed_event_attributes") for event in history.events):
                        break
                    await asyncio.sleep(0.02)
                else:
                    pytest.fail("unsupported result did not fail its workflow task")
                assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
            finally:
                await handle.terminate("test cleanup")
    # A premature telemetry completion is still permitted by the dated known
    # limitation; if one is emitted, its content must remain safe and bounded.
    spans = [span for span in exporter.get_finished_spans() if span.name == "temporal.workflow UnserializableResultWorkflow"]
    for span in spans:
        assert json.loads(span.attributes["output.value"]) == "object"
        assert not span.events
    assert len({span.context.span_id for span in spans}) <= 1


def application_history(history):
    """Command-bearing history, excluding server IDs/times and tracing headers."""
    events = []
    for event in history.events:
        if event.HasField("workflow_execution_started_event_attributes"):
            attrs = event.workflow_execution_started_event_attributes
            events.append(("start", attrs.input.SerializeToString(deterministic=True)))
        elif event.HasField("timer_started_event_attributes"):
            attrs = event.timer_started_event_attributes
            events.append(("timer", attrs.timer_id, attrs.start_to_fire_timeout.SerializeToString(deterministic=True)))
        elif event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            events.append(("activity", attrs.activity_type.name, attrs.activity_id,
                           attrs.input.SerializeToString(deterministic=True)))
        elif event.HasField("activity_task_completed_event_attributes"):
            attrs = event.activity_task_completed_event_attributes
            events.append(("activity-result", attrs.result.SerializeToString(deterministic=True)))
        elif event.HasField("workflow_execution_completed_event_attributes"):
            attrs = event.workflow_execution_completed_event_attributes
            events.append(("result", attrs.result.SerializeToString(deterministic=True)))
    return events


async def test_agent_on_off_identical_application_history_and_bidirectional_replay(environment, tracing):
    provider, exporter = tracing
    queue = str(uuid4())
    histories = []
    for enabled in (False, True):
        client = configured_client(environment, provider, TraceConfig()) if enabled else environment.client
        async with Worker(client, task_queue=queue, workflows=[DeterministicContentWorkflow], activities=[echo_content]):
            handle = await client.start_workflow(DeterministicContentWorkflow.run, {"message": "original"},
                                                 id=str(uuid4()), task_queue=queue)
            result = await handle.result()
            assert result["mutated"] is True
            history = await handle.fetch_history()
            histories.append(history)
    assert application_history(histories[0]) == application_history(histories[1])
    workflow_span = only(exporter, "temporal.workflow DeterministicContentWorkflow")
    # Input is a snapshot, not the application's subsequently-mutated dict.
    assert json.loads(workflow_span.attributes["input.value"]) == [{"message": "original"}]
    assert json.loads(workflow_span.attributes["output.value"]) == result
    count = len(exporter.get_finished_spans())
    for history in histories:
        original = history.to_json()
        for interceptors in ([], [TemporalInterceptor(provider)]):
            await Replayer(workflows=[DeterministicContentWorkflow], interceptors=interceptors).replay_workflow(history)
        assert history.to_json() == original
    assert len(exporter.get_finished_spans()) == count
