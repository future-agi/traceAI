"""Real framework contracts; no credentials, model APIs or remote collectors."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fi_instrumentation.instrumentation import TraceConfig, suppress_tracing
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext, models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage
from traceai_pydantic_ai import PydanticAIInstrumentor, instrument_pydantic_ai

SECRET = "private-payload-92734"


def new_provider(exporter=None):
    exporter = exporter or InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    previous = Agent._instrument_default
    Agent.instrument_all(False)
    inst = PydanticAIInstrumentor()
    if inst.is_instrumented_by_opentelemetry:
        inst.uninstrument()
    for key in (
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_INPUT_TEXT",
        "FI_HIDE_OUTPUT_TEXT",
        "FI_HIDE_INPUT_IMAGES",
        "FI_HIDE_ERROR_DETAILS",
        "FI_PII_REDACTION",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    if inst.is_instrumented_by_opentelemetry:
        inst.uninstrument()
    Agent.instrument_all(previous)


@pytest.fixture
def setup():
    providers = []

    def start(**kwargs):
        provider, exporter = new_provider()
        providers.append(provider)
        PydanticAIInstrumentor().instrument(tracer_provider=provider, **kwargs)
        assert PydanticAIInstrumentor().is_instrumented
        return provider, exporter

    yield start
    for provider in providers:
        provider.shutdown()


def by_kind(exporter, kind):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("gen_ai.span.kind") == kind
    ]


def usage(span, prefix="gen_ai.usage."):
    return tuple(
        span.attributes.get(prefix + key)
        for key in ("input_tokens", "output_tokens", "total_tokens")
    )


def assert_run_usage(exporter):
    spans = exporter.get_finished_spans()
    for agent in by_kind(exporter, "AGENT"):
        # Follow intermediate native tool/chain spans, but stop at nested agents.
        def belongs(model, agent=agent):
            parent = model.parent
            while parent is not None:
                if parent.span_id == agent.context.span_id:
                    return True
                found = next(
                    (s for s in spans if s.context.span_id == parent.span_id), None
                )
                if found is None or found.attributes.get("gen_ai.span.kind") == "AGENT":
                    return False
                parent = found.parent
            return False

        children = [s for s in by_kind(exporter, "LLM") if belongs(s)]
        # Native usage omits zero/unavailable dimensions; do not invent them.
        dimensions = [
            [usage(s)[i] for s in children if usage(s)[i] is not None] for i in range(2)
        ]
        inputs, outputs = (sum(values) if values else None for values in dimensions)
        total = inputs + outputs if inputs is not None and outputs is not None else None
        assert usage(agent, "gen_ai.aggregated_usage.") == (inputs, outputs, total)
        assert not any(k.startswith("gen_ai.usage.") for k in agent.attributes)


@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("method", ["run", "run_sync", "run_stream", "iter"])
def test_native_entrypoints_and_provider(setup, monkeypatch, builtin, method):
    global_provider, global_exporter = new_provider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: global_provider)
    originals = (Agent.run, Agent.run_sync, Agent.run_stream, Agent.iter)
    _provider, exporter = setup(use_builtin=builtin)
    assert (Agent.run, Agent.run_sync, Agent.run_stream, Agent.iter) == originals
    agent = Agent(TestModel(custom_output_text="hello"), name="example")

    async def execute():
        if method == "run":
            return (await agent.run("question")).output
        if method == "iter":
            async with agent.iter("question") as run:
                async for _ in run:
                    pass
                return run.result.output
        async with agent.run_stream("question") as result:
            chunks = [part async for part in result.stream_text()]
            return chunks[-1]

    result = (
        agent.run_sync("question").output
        if method == "run_sync"
        else asyncio.run(execute())
    )
    assert result == "hello"
    agents, llms = by_kind(exporter, "AGENT"), by_kind(exporter, "LLM")
    assert len(agents) == len(llms) == 1
    assert llms[0].parent.span_id == agents[0].context.span_id
    assert llms[0].attributes["gen_ai.request.model"] == "test"
    assert llms[0].attributes["gen_ai.system"] == "test"
    assert usage(llms[0])[0] > 0
    assert_run_usage(exporter)
    assert global_exporter.get_finished_spans() == ()
    global_provider.shutdown()


def test_convenience_and_lifecycle_do_not_replace_other_owners():
    provider, exporter = new_provider()
    previous = InstrumentationSettings(tracer_provider=provider)
    Agent.instrument_all(previous)
    inst = instrument_pydantic_ai(provider, True)
    settings = Agent._instrument_default
    assert PydanticAIInstrumentor() is inst
    inst.instrument(tracer_provider=provider)
    assert Agent._instrument_default is settings
    inst.uninstrument()
    assert Agent._instrument_default is previous
    # Saved native settings must not keep emitting after teardown.
    agent = Agent(TestModel())
    agent.instrument = settings
    agent.run_sync("hello")
    assert not exporter.get_finished_spans()
    inst.instrument(tracer_provider=provider)
    replacement = InstrumentationSettings(tracer_provider=provider)
    Agent.instrument_all(replacement)
    inst.uninstrument()
    assert Agent._instrument_default is replacement
    provider.shutdown()


def test_reinstrument_and_explicit_agent_optout(setup):
    provider, exporter = setup()
    agent = Agent(TestModel())
    agent.instrument = False
    agent.run_sync("hello")
    assert not exporter.get_finished_spans()
    inst = PydanticAIInstrumentor()
    inst.uninstrument()
    Agent(TestModel()).run_sync("hello")
    assert not exporter.get_finished_spans()
    inst.instrument(tracer_provider=provider)
    Agent(TestModel()).run_sync("hello")
    assert len(by_kind(exporter, "LLM")) == 1


def test_suppression_and_external_parent(setup):
    provider, exporter = setup()
    agent = Agent(TestModel())
    with suppress_tracing():
        agent.run_sync("hello")
    assert not exporter.get_finished_spans()
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        agent.run_sync("hello")
    assert (
        by_kind(exporter, "AGENT")[0].parent.span_id
        == parent.get_span_context().span_id
    )


def test_tools_retries_and_structured_result(setup):
    _, exporter = setup(include_content=True)
    calls = []

    class Answer(BaseModel):
        answer: str

    agent = Agent(TestModel(custom_output_args={"answer": "done"}), output_type=Answer)

    @agent.tool_plain(retries=1)
    def lookup(value: str) -> str:
        calls.append(value)
        if len(calls) == 1:
            raise ModelRetry("retry synthetic")
        return "tool result"

    result = agent.run_sync("question")
    assert result.output.answer == "done"
    assert len(calls) == 2
    tools = by_kind(exporter, "TOOL")
    assert (
        len([s for s in tools if s.attributes.get("gen_ai.tool.name") == "lookup"]) == 2
    )
    assert any("input.value" in s.attributes for s in tools)
    assert any(s.attributes.get("output.value") == "tool result" for s in tools)
    assert json.loads(by_kind(exporter, "AGENT")[0].attributes["output.value"]) == {
        "answer": "done"
    }
    assert_run_usage(exporter)


@pytest.mark.parametrize("method", ["run", "run_sync", "run_stream"])
def test_shared_usage_and_history_are_not_counted_again(setup, method):
    _, exporter = setup()
    agent = Agent(TestModel(custom_output_text="hello"))
    shared = RunUsage(input_tokens=100, output_tokens=50, requests=1)
    history = []
    before = (shared.input_tokens, shared.output_tokens)

    async def execute():
        nonlocal history
        for _ in range(2):
            if method == "run_stream":
                async with agent.run_stream(
                    "question", usage=shared, message_history=history
                ) as result:
                    await result.get_output()
                    history = result.all_messages()
            else:
                result = await agent.run(
                    "question", usage=shared, message_history=history
                )
                history = result.all_messages()

    if method == "run_sync":
        for _ in range(2):
            result = agent.run_sync("question", usage=shared, message_history=history)
            history = result.all_messages()
    else:
        asyncio.run(execute())
    assert len(by_kind(exporter, "AGENT")) == len(by_kind(exporter, "LLM")) == 2
    assert (
        sum(usage(s)[0] for s in by_kind(exporter, "LLM"))
        == shared.input_tokens - before[0]
    )
    assert (
        sum(usage(s)[1] for s in by_kind(exporter, "LLM"))
        == shared.output_tokens - before[1]
    )
    assert_run_usage(exporter)


def test_concurrent_shared_budget_has_run_local_totals(setup):
    _, exporter = setup()
    ready = asyncio.Event()
    arrived = 0

    async def response(messages, info):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        tokens = 11 if messages[-1].parts[0].content == "first" else 23
        return ModelResponse(
            parts=[TextPart("ok")],
            usage=RequestUsage(input_tokens=tokens, output_tokens=5),
        )

    async def execute():
        shared = RunUsage(input_tokens=100)
        agent = Agent(FunctionModel(response))
        await asyncio.gather(
            agent.run("first", usage=shared), agent.run("second", usage=shared)
        )

    asyncio.run(execute())
    assert sorted(usage(s)[0] for s in by_kind(exporter, "LLM")) == [11, 23]
    assert_run_usage(exporter)


def test_nested_agents_have_distinct_model_counts(setup):
    _, exporter = setup()
    child = Agent(TestModel(custom_output_text="child"), name="child")
    parent = Agent(TestModel(), name="parent")

    @parent.tool
    async def nested(ctx: RunContext[None]) -> str:
        return (await child.run("nested question", usage=ctx.usage)).output

    parent.run_sync("question")
    assert len(by_kind(exporter, "AGENT")) == 2
    assert len(by_kind(exporter, "LLM")) == 3
    assert_run_usage(exporter)


@pytest.mark.parametrize("source", ["model", "tool", "stream"])
def test_error_identity_and_redaction(setup, source):
    _, exporter = setup()
    error = ValueError(SECRET)

    def response(messages, info):
        raise error

    async def stream(messages, info):
        yield "first"
        raise error

    agent = Agent(FunctionModel(response, stream_function=stream))
    if source == "tool":
        agent = Agent(TestModel())

        @agent.tool_plain
        def fails() -> str:
            raise error

    async def execute():
        async with agent.run_stream(SECRET) as result:
            async for _ in result.stream_text():
                pass

    with pytest.raises(ValueError) as caught:
        if source == "stream":
            asyncio.run(execute())
        else:
            agent.run_sync(SECRET)
    assert caught.value is error
    spans = exporter.get_finished_spans()
    assert any(s.status.status_code == trace.StatusCode.ERROR for s in spans)
    assert SECRET not in repr(
        [
            (
                s.name,
                dict(s.attributes),
                s.status.description,
                [(e.name, dict(e.attributes)) for e in s.events],
            )
            for s in spans
        ]
    )


def test_cancelled_stream_finishes_spans(setup):
    _, exporter = setup()
    entered = asyncio.Event()

    async def stream(messages, info):
        yield "first"
        entered.set()
        await asyncio.Event().wait()

    async def execute():
        async def consume():
            async with Agent(FunctionModel(stream_function=stream)).run_stream(
                "hello"
            ) as result:
                async for _ in result.stream_text():
                    pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(execute())
    assert len(by_kind(exporter, "LLM")) == len(by_kind(exporter, "AGENT")) == 1
    assert all(s.end_time is not None for s in exporter.get_finished_spans())


@pytest.fixture
def collector():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    exporter = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", headers={}
    )
    provider, _ = new_provider(exporter)
    yield provider, received
    provider.shutdown()
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.mark.parametrize(
    "hide",
    [
        None,
        "hide_inputs",
        "hide_outputs",
        "hide_input_messages",
        "hide_output_messages",
        "hide_input_text",
        "hide_output_text",
        "hide_input_images",
    ],
)
def test_private_content_absent_on_otlp_wire(collector, hide):
    provider, received = collector
    config = TraceConfig(**{hide: True}) if hide else TraceConfig()
    instrument_pydantic_ai(provider, include_content=bool(hide), config=config)
    agent = Agent(TestModel(custom_output_text=SECRET), instructions=SECRET)

    @agent.tool_plain
    def lookup(value: str) -> str:
        """Synthetic tool description."""
        return SECRET

    agent.run_sync(SECRET)

    def fail(messages, info):
        raise ValueError(SECRET)

    with pytest.raises(ValueError):
        Agent(FunctionModel(fail)).run_sync(SECRET)
    assert provider.force_flush()
    assert received
    assert all(SECRET.encode() not in body for body in received)
    spans = [
        s
        for body in received
        for rs in ExportTraceServiceRequest.FromString(body).resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
    ]
    attrs = [{a.key: a.value for a in s.attributes} for s in spans]
    assert {a["gen_ai.span.kind"].string_value for a in attrs} >= {
        "AGENT",
        "LLM",
        "TOOL",
    }
    assert any("gen_ai.usage.input_tokens" in a for a in attrs)
    assert any("gen_ai.aggregated_usage.input_tokens" in a for a in attrs)
    assert all(
        "model_request_parameters" not in a and "pydantic_ai.all_messages" not in a
        for a in attrs
    )


def test_content_optin_and_pii_redaction(collector):
    provider, received = collector
    instrument_pydantic_ai(
        provider, include_content=True, config=TraceConfig(pii_redaction=True)
    )
    Agent(TestModel(custom_output_text="answer person@example.com")).run_sync(
        "question person@example.com"
    )
    provider.force_flush()
    wire = b"".join(received)
    assert b"question" in wire and b"answer" in wire
    assert b"person@example.com" not in wire


def test_env_privacy_flag_wins_over_content_optin(setup, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    _, exporter = setup(include_content=True)
    Agent(TestModel(custom_output_text=SECRET)).run_sync(SECRET)
    assert SECRET not in repr(
        [dict(s.attributes) for s in exporter.get_finished_spans()]
    )


@pytest.mark.parametrize("include_content", [False, True])
def test_cache_and_reasoning_usage_not_double_counted(setup, include_content):
    _, exporter = setup(include_content=include_content)

    def response(messages, info):
        return ModelResponse(
            parts=[TextPart("ok")],
            usage=RequestUsage(
                input_tokens=20,
                output_tokens=10,
                cache_read_tokens=8,
                cache_write_tokens=3,
                details={
                    "reasoning_tokens": 4,
                    "cache_read_tokens": 8,
                    "cache_write_tokens": 3,
                    SECRET: 9,
                },
            ),
        )

    Agent(FunctionModel(response)).run_sync("hello")
    model = by_kind(exporter, "LLM")[0]
    assert usage(model) == (20, 10, 30)
    assert model.attributes["gen_ai.usage.cache_read.input_tokens"] == 8
    assert model.attributes["gen_ai.usage.cache_creation.input_tokens"] == 3
    assert model.attributes["gen_ai.usage.details.reasoning_tokens"] == 4
    assert SECRET not in repr(dict(model.attributes))
    assert_run_usage(exporter)


def test_default_provider_and_public_exports(monkeypatch):
    import traceai_pydantic_ai

    provider, exporter = new_provider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    instrument_pydantic_ai()
    Agent(TestModel()).run_sync("hello")
    assert len(by_kind(exporter, "LLM")) == 1
    for name in traceai_pydantic_ai.__all__:
        assert getattr(traceai_pydantic_ai, name) is not None
    provider.shutdown()


@pytest.mark.parametrize(
    "options", [{"include_content": "false"}, {"config": object()}, {"config": {}}]
)
def test_invalid_config_does_not_change_native_owner(options):
    previous = Agent._instrument_default
    with pytest.raises(TypeError):
        PydanticAIInstrumentor().instrument(**options)
    assert Agent._instrument_default is previous
    assert not PydanticAIInstrumentor().is_instrumented


def test_uninstrument_during_stream_preserves_completion(setup):
    _, exporter = setup()

    async def execute():
        async with Agent(TestModel(custom_output_text="done")).run_stream(
            "hello"
        ) as result:
            PydanticAIInstrumentor().uninstrument()
            return await result.get_output()

    assert asyncio.run(execute()) == "done"
    assert len(by_kind(exporter, "AGENT")) == len(by_kind(exporter, "LLM")) == 1
    assert_run_usage(exporter)


def test_native_projection_hides_sampler_inputs_and_unknown_events():
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler

    seen = []

    class InspectSampler(Sampler):
        def should_sample(self, *args, **kwargs):
            assert SECRET not in repr(args) + repr(kwargs)
            seen.append(True)
            return ALWAYS_ON.should_sample(*args, **kwargs)

        def get_description(self):
            return "inspect"

    provider = TracerProvider(sampler=InspectSampler())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    PydanticAIInstrumentor().instrument(tracer_provider=provider)
    tracer = Agent._instrument_default.tracer
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "test",
        "gen_ai.input.messages": SECRET,
        "model_request_parameters": SECRET,
        "gen_ai.usage.input_tokens": 7,
        "gen_ai.usage.output_tokens": 3,
    }
    with tracer.start_as_current_span(SECRET, attributes=attrs) as span:
        span.add_event(SECRET, {SECRET: SECRET})
        span.update_name(SECRET)
        span.set_attribute("pydantic_ai.metadata", SECRET)
        span.add_link(trace.SpanContext(1, 1, False), {SECRET: SECRET})
        span.end()
        span.end()
    assert len(by_kind(exporter, "LLM")) == 1
    model = by_kind(exporter, "LLM")[0]
    assert usage(model) == (7, 3, 10)
    assert SECRET not in repr(
        (model.name, dict(model.attributes), model.events, model.links)
    )
    assert seen
    provider.shutdown()


def test_structured_stream_retains_native_result(setup):
    _, exporter = setup(include_content=True)

    class Answer(BaseModel):
        answer: str

    async def execute():
        agent = Agent(
            TestModel(custom_output_args={"answer": "done"}), output_type=Answer
        )
        async with agent.run_stream("question") as result:
            return await result.get_output()

    assert asyncio.run(execute()).answer == "done"
    assert len(by_kind(exporter, "AGENT")) == len(by_kind(exporter, "LLM")) == 1
    assert_run_usage(exporter)


def test_no_metrics_or_logs_provider_used(setup, monkeypatch):
    import pydantic_ai.models.instrumented as native

    monkeypatch.setattr(
        native, "get_meter_provider", lambda: pytest.fail("global meter used")
    )
    setup()
    settings = Agent._instrument_default
    assert settings.include_content is False
    assert settings.include_binary_content is False
    Agent(TestModel()).run_sync("hello")
