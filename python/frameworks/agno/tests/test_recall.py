"""Offline integration recall checks against installed, real Agno/OpenAI/OpenInference."""

import asyncio
import json
import os

import httpx
import pytest
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.run.cancel import cancel_run
from agno.team import Team
from agno.tools.function import Function, FunctionCall
from openai import AsyncOpenAI, OpenAI
from openinference.instrumentation import TraceConfig
from opentelemetry import context, trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_agno import configure_agno_tracing

os.environ["AGNO_TELEMETRY"] = "false"
SECRET = "SYNTHETIC_PRIVATE_TOOL_ARGUMENT_484"


class Tracker(SpanProcessor):
    def __init__(self):
        self.started = []

    def on_start(self, span, parent_context=None):
        self.started.append(span)


@pytest.fixture
def harness():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    tracker = Tracker()
    provider.add_span_processor(tracker)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = {"cancel": None, "tools": False, "requests": 0}

    def handler(request):
        settings["requests"] += 1
        body = json.loads(request.content)
        base = {"id": "chatcmpl-synthetic", "created": 0, "model": body["model"]}
        if settings["cancel"]:
            assert cancel_run(settings["cancel"])
        if body.get("stream"):
            payload = "".join(
                "data: "
                + json.dumps(
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": finish}
                        ],
                    }
                )
                + "\n\n"
                for delta, finish in [
                    ({"role": "assistant", "content": "Par"}, None),
                    ({"content": "is"}, None),
                    ({}, "stop"),
                ]
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=payload + "data: [DONE]\n\n",
            )
        msg = {"role": "assistant", "content": "Paris"}
        finish = "stop"
        if (
            settings["tools"]
            and body.get("tools")
            and not any(m["role"] == "tool" for m in body["messages"])
        ):
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "synthetic-call",
                        "type": "function",
                        "function": {
                            "name": body["tools"][0]["function"]["name"],
                            "arguments": json.dumps({"text": SECRET}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        return httpx.Response(
            200,
            json={
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    sync_http = httpx.Client(transport=httpx.MockTransport(handler))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sync = OpenAI(
        api_key="synthetic-key",
        base_url="https://model.invalid/v1",
        http_client=sync_http,
    )
    async_client = AsyncOpenAI(
        api_key="synthetic-key",
        base_url="https://model.invalid/v1",
        http_client=async_http,
    )

    def model():
        return OpenAIChat(id="gpt-4o-mini", client=sync, async_client=async_client)

    yield provider, exporter, tracker, model, settings
    from traceai_agno import AgnoInstrumentorWrapper

    AgnoInstrumentorWrapper().uninstrument()
    provider.shutdown()
    sync_http.close()
    asyncio.run(async_http.aclose())


def span_summary(exporter):
    return [
        {
            "name": s.name,
            "status": s.status.status_code.name,
            "kind": s.attributes.get("openinference.span.kind"),
            "trace_id": s.context.trace_id,
            "parent": s.parent.span_id if s.parent else None,
            "attributes": dict(s.attributes),
        }
        for s in exporter.get_finished_spans()
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_cancelled_run_is_not_ok(harness, asynchronous, streaming):
    provider, exporter, tracker, model, settings = harness
    settings["cancel"] = "synthetic-cancel-484"
    configure_agno_tracing(tracer_provider=provider)
    agent = Agent(model=model(), telemetry=False)
    kwargs = dict(run_id=settings["cancel"], stream=streaming, yield_run_output=True)
    if asynchronous:
        result = (
            [r async for r in agent.arun("cancel me", **kwargs)][-1]
            if streaming
            else await agent.arun("cancel me", **kwargs)
        )
    else:
        result = (
            list(agent.run("cancel me", **kwargs))[-1]
            if streaming
            else agent.run("cancel me", **kwargs)
        )
    assert result.status.value == "CANCELLED", result.status
    agent_span = next(
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("openinference.span.kind") == "AGENT"
    )
    assert agent_span.status.status_code != trace.StatusCode.OK, span_summary(exporter)


def test_hidden_inputs_exclude_tool_arguments(harness):
    provider, exporter, tracker, model, settings = harness
    settings["tools"] = True

    def lookup(text: str) -> str:
        """Look up an example record."""
        return "found"

    configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    result = Agent(model=model(), tools=[lookup], telemetry=False).run(
        "private request"
    )
    assert result.content == "Paris"
    leaked = [
        (s.name, k, v)
        for s in exporter.get_finished_spans()
        for k, v in s.attributes.items()
        if SECRET in str(v)
    ]
    assert not leaked, leaked


@pytest.mark.asyncio
async def test_async_stream_suppression_does_not_break_agent(harness):
    provider, exporter, tracker, model, settings = harness
    agent = Agent(model=model(), telemetry=False)
    baseline = [chunk.content async for chunk in agent.arun("test", stream=True)]
    configure_agno_tracing(tracer_provider=provider)
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        actual = [chunk.content async for chunk in agent.arun("test", stream=True)]
    finally:
        context.detach(token)
    assert actual == baseline
    assert not exporter.get_finished_spans()


def test_callable_tools_not_invoked_without_run_context(harness):
    provider, exporter, tracker, model, settings = harness
    calls = []

    def tools(run_context: RunContext):
        calls.append(run_context.run_id)
        return []

    baseline = Agent(model=model(), tools=tools, telemetry=False).run("test")
    assert baseline.content == "Paris"
    configure_agno_tracing(tracer_provider=provider)
    actual = Agent(model=model(), tools=tools, telemetry=False).run("test")
    assert actual.content == baseline.content
    assert len(calls) == 2


def test_streaming_tool_error_is_not_swallowed(harness):
    provider, exporter, tracker, model, settings = harness

    def broken_tool():
        """Yield one piece then fail."""
        yield "first"
        raise ValueError("synthetic tool failure")

    def invoke():
        fc = FunctionCall(function=Function.from_callable(broken_tool), arguments={})
        response = fc.execute()
        return list(response.result)

    with pytest.raises(ValueError, match="synthetic tool failure"):
        invoke()
    configure_agno_tracing(tracer_provider=provider)
    with pytest.raises(ValueError, match="synthetic tool failure"):
        invoke()


def test_team_honors_remote_parent(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    parent = trace.SpanContext(
        trace_id=0x112233445566,
        span_id=0x22334455,
        is_remote=True,
        trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
    )
    with trace.use_span(trace.NonRecordingSpan(parent)):
        assert (
            Team(model=model(), members=[], telemetry=False).run("test").content
            == "Paris"
        )
    team_span = next(
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("openinference.span.kind") == "AGENT"
    )
    assert (
        team_span.context.trace_id == parent.trace_id and team_span.parent == parent
    ), span_summary(exporter)


def test_sync_stream_does_not_leak_current_span(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    before = trace.get_current_span().get_span_context()
    stream = Agent(model=model(), telemetry=False).run("test", stream=True)
    next(stream)
    during = trace.get_current_span().get_span_context()
    # This is an independent caller operation, not a tool executed by the stream.
    assert (
        Agent(name="Independent", model=model(), telemetry=False)
        .run("second request")
        .content
        == "Paris"
    )
    stream.close()
    independent = next(
        s for s in exporter.get_finished_spans() if s.name == "Independent.run"
    )
    assert independent.parent is None and during == before, {
        "before": str(before),
        "during": str(during),
        "spans": span_summary(exporter),
    }


@pytest.mark.asyncio
async def test_async_close_does_not_report_success(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    stream = Agent(model=model(), telemetry=False).arun("test", stream=True)
    await anext(stream)
    await stream.aclose()
    # Public stream closure can defer nested generator finalization to the event loop.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    completed = exporter.get_finished_spans()
    agent_spans = [
        s for s in completed if s.attributes.get("openinference.span.kind") == "AGENT"
    ]
    assert agent_spans, {
        "started": len(tracker.started),
        "completed": span_summary(exporter),
    }
    assert all(s.status.status_code != trace.StatusCode.OK for s in agent_spans), (
        span_summary(exporter)
    )


def test_sync_stream_suppression_preserves_output(harness):
    provider, exporter, tracker, model, settings = harness
    agent = Agent(model=model(), telemetry=False)
    baseline = [c.content for c in agent.run("test", stream=True)]
    configure_agno_tracing(tracer_provider=provider)
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        actual = [c.content for c in agent.run("test", stream=True)]
    finally:
        context.detach(token)
    assert actual == baseline


def test_model_auth_headers_not_exported(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    m = model()
    m.extra_headers = {
        "Authorization": "Bearer SYNTHETIC_AUTH_484",
        "X-Api-Key": "SYNTHETIC_AUTH_484",
    }
    assert Agent(model=m, telemetry=False).run("test").content == "Paris"
    leaks = [
        (s.name, k, v)
        for s in exporter.get_finished_spans()
        for k, v in s.attributes.items()
        if "SYNTHETIC_AUTH_484" in str(v)
    ]
    assert not leaks, leaks


def test_workflow_optional_name(harness):
    from agno.workflow import Workflow

    provider, exporter, tracker, model, settings = harness
    baseline = Workflow(
        steps=[Agent(model=model(), telemetry=False)], telemetry=False
    ).run("test")
    assert baseline.content == "Paris"
    configure_agno_tracing(tracer_provider=provider)
    result = Workflow(
        steps=[Agent(model=model(), telemetry=False)], telemetry=False
    ).run("test")
    assert result.content == baseline.content


def test_callable_workflow(harness):
    from agno.workflow import Workflow

    provider, exporter, tracker, model, settings = harness

    def steps(workflow, execution_input, **kwargs):
        return "Paris"

    baseline = Workflow(name="Custom", steps=steps, telemetry=False).run("test")
    assert baseline.content == "Paris"
    configure_agno_tracing(tracer_provider=provider)
    result = Workflow(name="Custom", steps=steps, telemetry=False).run("test")
    assert result.content == baseline.content


@pytest.mark.asyncio
async def test_async_tool_failure_finishes_span(harness):
    provider, exporter, tracker, model, settings = harness

    async def broken_tool():
        """Yield one piece then fail."""
        yield "first"
        raise ValueError("synthetic async tool failure")

    configure_agno_tracing(tracer_provider=provider)
    fc = FunctionCall(function=Function.from_callable(broken_tool), arguments={})
    response = await fc.aexecute()
    with pytest.raises(ValueError, match="synthetic async tool failure"):
        _ = [piece async for piece in response.result]
    assert len(exporter.get_finished_spans()) == 1, {
        "started": len(tracker.started),
        "finished": span_summary(exporter),
    }
    assert exporter.get_finished_spans()[0].status.status_code == trace.StatusCode.ERROR


@pytest.mark.asyncio
async def test_cancelled_async_tool_finishes_span(harness):
    provider, exporter, tracker, model, settings = harness
    entered = asyncio.Event()

    async def long_tool():
        """Wait for cancellation."""
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    configure_agno_tracing(tracer_provider=provider)
    fc = FunctionCall(function=Function.from_callable(long_tool), arguments={})
    task = asyncio.create_task(fc.aexecute())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(exporter.get_finished_spans()) == 1, {
        "started": len(tracker.started),
        "finished": span_summary(exporter),
    }


@pytest.mark.parametrize("hidden", [False, True])
def test_auth_redaction_is_recursive_and_does_not_mutate_request(harness, hidden):
    from copy import deepcopy

    from traceai_agno._wrappers._model_wrapper import _filter_sensitive_params

    params = {
        "temperature": 0.3,
        "max_tokens": 100,
        "token": "SYNTHETIC_AUTH_484",
        "access_token": "SYNTHETIC_AUTH_484",
        "extra_headers": {"X-Custom-Gateway-Credential": "SYNTHETIC_AUTH_484"},
        "nested": [
            {
                "API-Key": "SYNTHETIC_AUTH_484",
                "Authorization": "SYNTHETIC_AUTH_484",
                "cookie": "SYNTHETIC_AUTH_484",
                "aws_secret_access_key": "SYNTHETIC_AUTH_484",
                "safe": "retained",
            }
        ],
    }
    original = deepcopy(params)
    cleaned = _filter_sensitive_params(params)
    assert "SYNTHETIC_AUTH_484" not in json.dumps(cleaned)
    assert cleaned["temperature"] == 0.3
    assert cleaned["max_tokens"] == 100
    assert cleaned["nested"][0]["safe"] == "retained"
    assert params == original
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    m = model()
    headers = {
        "Authorization": "Bearer SYNTHETIC_AUTH_484",
        "X-Custom-Gateway-Credential": "SYNTHETIC_AUTH_484",
    }
    m.extra_headers = dict(headers)
    assert Agent(model=m, telemetry=False).run("test").content == "Paris"
    assert m.extra_headers == headers
    assert all(
        "SYNTHETIC_AUTH_484" not in str(s.attributes)
        for s in exporter.get_finished_spans()
    )


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("environment", [False, True])
def test_tool_parameters_follow_input_capture_setting(
    harness, monkeypatch, hidden, environment
):
    provider, exporter, tracker, model, settings = harness
    if environment:
        monkeypatch.setenv("OPENINFERENCE_HIDE_INPUTS", str(hidden).lower())
        config = None
    else:
        config = TraceConfig(hide_inputs=hidden)
    configure_agno_tracing(tracer_provider=provider, config=config)

    def echo(text: str) -> str:
        """Return an acknowledgement."""
        return "done"

    fc = FunctionCall(function=Function.from_callable(echo), arguments={"text": SECRET})
    assert fc.execute().result == "done"
    span = exporter.get_finished_spans()[0]
    assert (SECRET in str(span.attributes)) is (not hidden)
    assert span.attributes["tool.name"] == "echo"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("callable_steps", [False, True])
@pytest.mark.asyncio
async def test_optional_workflow_metadata_all_paths(
    harness, asynchronous, streaming, callable_steps
):
    from agno.workflow import Workflow

    provider, exporter, tracker, model, settings = harness

    def steps(workflow, execution_input, **kwargs):
        return "Paris"

    def workflow():
        return Workflow(
            steps=steps if callable_steps else [Agent(model=model(), telemetry=False)],
            telemetry=False,
        )

    async def run(wf):
        if asynchronous:
            result = (
                [r async for r in wf.arun("test", stream=True)]
                if streaming
                else await wf.arun("test")
            )
        else:
            result = list(wf.run("test", stream=True)) if streaming else wf.run("test")
        return (
            [(type(r).__name__, getattr(r, "content", None)) for r in result]
            if streaming
            else result.content
        )

    expected = await run(workflow())
    configure_agno_tracing(tracer_provider=provider)
    assert await run(workflow()) == expected
    assert exporter.get_finished_spans()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.asyncio
async def test_completed_stream_restores_caller_and_keeps_children(
    harness, asynchronous, team
):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    obj = (
        Team(name="Stream", model=model(), members=[], telemetry=False)
        if team
        else Agent(name="Stream", model=model(), telemetry=False)
    )
    with provider.get_tracer("caller").start_as_current_span("caller") as caller:
        expected = caller.get_span_context()
        chunks = []
        if asynchronous:
            stream = obj.arun("test", stream=True)
            async for chunk in stream:
                chunks.append(chunk.content)
                assert trace.get_current_span().get_span_context() == expected
                await asyncio.sleep(0)
        else:
            stream = obj.run("test", stream=True)
            for chunk in stream:
                chunks.append(chunk.content)
                assert trace.get_current_span().get_span_context() == expected
        assert "".join(c or "" for c in chunks) == "Paris"
        assert trace.get_current_span().get_span_context() == expected
    spans = exporter.get_finished_spans()
    agent = next(
        s for s in spans if s.attributes.get("openinference.span.kind") == "AGENT"
    )
    llm = next(s for s in spans if s.attributes.get("openinference.span.kind") == "LLM")
    assert agent.parent == expected
    assert llm.parent == agent.context
    assert agent.status.status_code == trace.StatusCode.OK
    assert len(spans) == len(tracker.started) == 3
    assert not trace.get_current_span().get_span_context().is_valid


def test_team_respects_unsampled_remote_parent(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    parent = trace.SpanContext(
        trace_id=0x12345678,
        span_id=0x22334455,
        is_remote=True,
        trace_flags=trace.TraceFlags(0),
    )
    with trace.use_span(trace.NonRecordingSpan(parent)):
        assert (
            Team(model=model(), members=[], telemetry=False).run("test").content
            == "Paris"
        )
        assert trace.get_current_span().get_span_context() == parent
    assert not exporter.get_finished_spans()
    assert not tracker.started


@pytest.mark.asyncio
async def test_async_nonstream_suppression_preserves_result(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        assert (
            await Agent(model=model(), telemetry=False).arun("test")
        ).content == "Paris"
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_tool_stream_context_and_lifetime(harness, asynchronous, fail):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)

    def pieces():
        """Yield two pieces with an optional error."""
        yield "first"
        if fail:
            raise ValueError("synthetic failure")
        yield "second"

    async def apieces():
        """Yield two pieces with an optional error."""
        yield "first"
        if fail:
            raise ValueError("synthetic failure")
        yield "second"

    fc = FunctionCall(
        function=Function.from_callable(apieces if asynchronous else pieces),
        arguments={},
    )
    response = await fc.aexecute() if asynchronous else fc.execute()
    before = trace.get_current_span().get_span_context()
    result = []
    try:
        if asynchronous:
            async for item in response.result:
                result.append(item)
                assert trace.get_current_span().get_span_context() == before
        else:
            for item in response.result:
                result.append(item)
                assert trace.get_current_span().get_span_context() == before
    except ValueError as exc:
        assert fail and str(exc) == "synthetic failure"
    else:
        assert not fail
    assert result == (["first"] if fail else ["first", "second"])
    assert len(tracker.started) == len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == (
        trace.StatusCode.ERROR if fail else trace.StatusCode.OK
    )


def test_delegate_callback_and_framework_methods_restore(harness):
    from agno.agent import _run
    from openinference.instrumentation.agno import AgnoInstrumentor
    from traceai_agno import AgnoInstrumentorWrapper

    provider, exporter, tracker, model, settings = harness
    delegate = AgnoInstrumentor()
    original_callback = delegate._instrument.__func__
    original_run = _run._run
    original_tool = FunctionCall.execute
    for _ in range(2):
        configure_agno_tracing(tracer_provider=provider)
        assert delegate._instrument.__func__ is original_callback
        assert delegate.is_instrumented_by_opentelemetry
        assert Agent(model=model(), telemetry=False).run("test").content == "Paris"
        AgnoInstrumentorWrapper().uninstrument()
        assert _run._run is original_run
        assert FunctionCall.execute is original_tool
    assert len(exporter.get_finished_spans()) == 4


def test_failed_partial_installation_restores_framework(harness, monkeypatch):
    from agno.agent import _run
    from openinference.instrumentation.agno import AgnoInstrumentor
    from traceai_agno._wrappers import _install

    provider, exporter, tracker, model, settings = harness
    original = _run._run
    original_callback = AgnoInstrumentor()._instrument.__func__
    real_wrap = _install.wrap_function_wrapper
    calls = 0

    def fail_after_wrapping(*args):
        nonlocal calls
        calls += 1
        real_wrap(*args)
        if calls == 5:
            raise RuntimeError("synthetic activation failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(_install, "wrap_function_wrapper", fail_after_wrapping)
        with pytest.raises(RuntimeError, match="synthetic activation failure"):
            configure_agno_tracing(tracer_provider=provider)
    assert _run._run is original
    assert AgnoInstrumentor()._instrument.__func__ is original_callback
    assert not AgnoInstrumentor().is_instrumented_by_opentelemetry
    configure_agno_tracing(tracer_provider=provider)
    assert Agent(model=model(), telemetry=False).run("test").content == "Paris"
    assert len(exporter.get_finished_spans()) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_early_tool_close_ends_span_and_preserves_context(harness, asynchronous):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    closed = []

    def pieces():
        """Yield until the consumer closes the stream."""
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(True)

    async def apieces():
        """Yield until the consumer closes the stream."""
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(True)

    fc = FunctionCall(
        function=Function.from_callable(apieces if asynchronous else pieces),
        arguments={},
    )
    response = await fc.aexecute() if asynchronous else fc.execute()
    parent = trace.get_current_span().get_span_context()
    if asynchronous:
        assert await anext(response.result) == "first"
        await response.result.aclose()
    else:
        assert next(response.result) == "first"
        response.result.close()
    assert closed == [True]
    assert trace.get_current_span().get_span_context() == parent
    assert len(tracker.started) == len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code != trace.StatusCode.OK


def test_distribution_includes_upstream_license_and_notice():
    from importlib.resources import files

    package = files("traceai_agno._wrappers")
    assert "Apache License" in package.joinpath("LICENSE").read_text()
    assert (
        "OpenInference instrumentation for Agno 1.1.0"
        in package.joinpath("NOTICE").read_text()
    )
