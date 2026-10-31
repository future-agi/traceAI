import asyncio
import inspect
import json
from importlib.metadata import distribution

import pytest
from agentscope import model, pipeline
from fi_instrumentation import suppress_tracing, using_session, using_user
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .conftest import (
    API_KEY,
    V2,
    call_agent,
    call_tool,
    make_agent,
    make_model,
    make_toolkit,
    model_input,
    msg,
    response,
)


def spans_by_kind(exporter, kind):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("gen_ai.span.kind") == kind
    ]


@pytest.mark.parametrize("stream", [False, True])
async def test_real_model_endpoint_usage_and_stream_parity(tracing, endpoint, stream):
    instrumentor, provider, exporter = tracing
    instrumentor.uninstrument()
    baseline = await make_model(endpoint, stream)(model_input())
    if stream:
        baseline = [c async for c in baseline]
    instrumentor.instrument(tracer_provider=provider)
    result = await make_model(endpoint, stream)(model_input())
    if stream:
        assert inspect.isasyncgen(result)
        result = [c async for c in result]
        assert [instrumentor._attributes.content(c) for c in result] == [
            instrumentor._attributes.content(c) for c in baseline
        ]
    else:
        assert type(result) is type(baseline)
        assert instrumentor._attributes.content(
            result
        ) == instrumentor._attributes.content(baseline)
    llm = spans_by_kind(exporter, "LLM")
    assert len(llm) == 1
    attrs = llm[0].attributes
    assert attrs["gen_ai.request.model"] == "local-test"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 7
    assert attrs["output.value"] == "hello local"
    assert attrs["gen_ai.input.messages.0.message.content"] == "question"
    assert llm[0].status.status_code is StatusCode.OK
    assert len(endpoint.requests) == 2


async def test_real_agent_tool_roundtrip_parentage(tracing, endpoint):
    _, _, exporter = tracing
    endpoint.mode = "tool"
    agent = make_agent(endpoint, toolkit=make_toolkit())
    with using_session("session-1"), using_user("user-1"):
        result = await call_agent(agent)
    assert result is not None
    agents, tools, llms = [
        spans_by_kind(exporter, kind) for kind in ("AGENT", "TOOL", "LLM")
    ]
    assert len(agents) == len(tools) == 1
    assert len(llms) == 2
    assert tools[0].parent.span_id == agents[0].context.span_id
    assert all(s.parent.span_id == agents[0].context.span_id for s in llms)
    assert tools[0].attributes["gen_ai.tool.name"] == "add"
    assert tools[0].attributes["gen_ai.tool.call.id"] == "call-1"
    assert tools[0].attributes["output.value"] == "7"
    assert agents[0].attributes["gen_ai.agent.name"] == "assistant"
    assert all(s.attributes["session.id"] == "session-1" for s in agents + tools + llms)
    assert len(endpoint.requests) == 2


async def test_real_tool_stream_parity(tracing):
    instrumentor, provider, exporter = tracing
    toolkit = make_toolkit()
    instrumentor.uninstrument()
    baseline = call_tool(toolkit)
    if inspect.isawaitable(baseline):
        baseline = await baseline
    baseline = [c async for c in baseline]
    instrumentor.instrument(tracer_provider=provider)
    result = call_tool(toolkit)
    if inspect.isawaitable(result):
        result = await result
    chunks = [c async for c in result]
    assert [instrumentor._attributes.content(c) for c in chunks] == [
        instrumentor._attributes.content(c) for c in baseline
    ]
    assert len(spans_by_kind(exporter, "TOOL")) == 1


async def test_pipeline_chain_and_agent_parentage(tracing, endpoint):
    _, _, exporter = tracing
    agent = make_agent(endpoint)
    if V2:
        instance = pipeline.TeamPipeline(leader=agent, members=[])
        awaitable = instance.reply_stream(msg())
        events = [item async for item in awaitable]
        assert events
    else:
        instance = pipeline.SequentialPipeline([agent])
        assert await instance(msg()) is not None
    chains = spans_by_kind(exporter, "CHAIN")
    agents = spans_by_kind(exporter, "AGENT")
    assert len(chains) == len(agents) == 1
    assert agents[0].parent.span_id == chains[0].context.span_id
    assert len(spans_by_kind(exporter, "LLM")) == 1


@pytest.mark.skipif(V2, reason="1.x functional pipeline API")
async def test_fanout_function_parentage(tracing, endpoint):
    _, _, exporter = tracing
    agents = [make_agent(endpoint, name=f"a{i}") for i in range(2)]
    values = await pipeline.fanout_pipeline(agents, msg())
    assert len(values) == 2
    (chain,) = spans_by_kind(exporter, "CHAIN")
    assert len(spans_by_kind(exporter, "AGENT")) == 2
    assert all(
        s.parent.span_id == chain.context.span_id
        for s in spans_by_kind(exporter, "AGENT")
    )


@pytest.mark.skipif(V2, reason="1.x subclass agent API")
async def test_future_custom_agent_direct_reply_identity_and_error(tracing):
    from agentscope.agent import AgentBase

    _, _, exporter = tracing
    value = msg("answer", "assistant")
    error = ValueError("sensitive-agent-error")

    class CustomAgent(AgentBase):
        async def reply(self, x):
            if x is None:
                raise error
            return value

    agent = CustomAgent()
    assert await agent.reply(msg()) is value
    with pytest.raises(ValueError) as caught:
        await agent.reply(None)
    assert caught.value is error
    spans = spans_by_kind(exporter, "AGENT")
    assert len(spans) == 2
    assert spans[-1].status.status_code is StatusCode.ERROR


async def test_agents_and_models_created_before_instrument(tracing, endpoint):
    instrumentor, provider, exporter = tracing
    instrumentor.uninstrument()
    agent = make_agent(endpoint)
    instrumentor.instrument(tracer_provider=provider)
    await call_agent(agent)
    assert len(spans_by_kind(exporter, "AGENT")) == 1
    assert len(spans_by_kind(exporter, "LLM")) == 1


async def test_idempotent_uninstrument_reinstrument(tracing, endpoint):
    instrumentor, provider, exporter = tracing
    original_provider = trace.get_tracer_provider()
    instrumentor.instrument(tracer_provider=provider)
    agent = make_agent(endpoint)
    await call_agent(agent)
    assert len(spans_by_kind(exporter, "AGENT")) == 1
    assert trace.get_tracer_provider() is original_provider
    instrumentor.uninstrument()
    await call_agent(agent)
    assert len(spans_by_kind(exporter, "AGENT")) == 1
    instrumentor.instrument(tracer_provider=provider)
    await call_agent(agent)
    assert len(spans_by_kind(exporter, "AGENT")) == 2
    assert len(spans_by_kind(exporter, "LLM")) == 2


async def test_suppression_no_spans(tracing, endpoint):
    _, _, exporter = tracing
    with suppress_tracing():
        await call_agent(make_agent(endpoint))
    assert exporter.get_finished_spans() == ()


async def test_concurrent_agents_isolated_parentage(tracing, endpoint):
    _, _, exporter = tracing
    await asyncio.gather(
        *(call_agent(make_agent(endpoint, name=f"agent-{i}")) for i in range(3))
    )
    agents = spans_by_kind(exporter, "AGENT")
    llms = spans_by_kind(exporter, "LLM")
    assert len(agents) == len(llms) == 3
    assert {s.parent.span_id for s in llms} == {s.context.span_id for s in agents}
    assert all(s.parent is None for s in agents)
    assert not trace.get_current_span().get_span_context().is_valid


async def test_model_error_baseline_parity(tracing, endpoint):
    instrumentor, provider, exporter = tracing
    endpoint.status = 400
    instrumentor.uninstrument()
    with pytest.raises(Exception) as baseline:
        await make_model(endpoint)(model_input())
    instrumentor.instrument(tracer_provider=provider)
    with pytest.raises(type(baseline.value)) as result:
        await make_model(endpoint)(model_input())
    assert str(result.value) == str(baseline.value)
    assert len(endpoint.requests) == 2
    spans = spans_by_kind(exporter, "LLM")
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].attributes["error.type"].endswith("BadRequestError")
    assert spans[0].status.description is None


async def test_future_custom_model_identity_and_exception(tracing):
    _, _, exporter = tracing
    value = response()
    error = ValueError("private application text")

    class CustomModel(model.ChatModelBase):
        def __init__(self):
            self.model_name = self.model = "custom"
            self.fail = False

        async def __call__(self, *args, **kwargs):
            if self.fail:
                raise error
            return value

    instance = CustomModel()
    assert await instance([]) is value
    instance.fail = True
    with pytest.raises(ValueError) as caught:
        await instance([])
    assert caught.value is error
    assert len(spans_by_kind(exporter, "LLM")) == 2
    exported = str(
        [
            (dict(s.attributes), s.status.description, s.events)
            for s in exporter.get_finished_spans()
        ]
    )
    assert "private application text" not in exported


async def test_fail_open_capture_and_span_start(tracing, monkeypatch):
    instrumentor, _, exporter = tracing
    value = response()
    calls = []

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            calls.append(1)
            return value

    instance = CustomModel.__new__(CustomModel)
    monkeypatch.setattr(instrumentor._attributes, "request", lambda *a: 1 / 0)
    assert await instance([]) is value
    monkeypatch.setattr(instrumentor._tracer, "start_span", lambda *a, **k: 1 / 0)
    assert await instance([]) is value
    assert calls == [1, 1]
    assert len(spans_by_kind(exporter, "LLM")) == 1


@pytest.mark.parametrize(
    "flag",
    [
        "hide_inputs",
        "hide_input_text",
        "hide_input_messages",
        "hide_outputs",
        "hide_output_text",
        "hide_output_messages",
    ],
)
async def test_privacy_hide_flags(retrace, endpoint, flag):
    instrumentor, _, exporter = retrace(**{flag: True})
    endpoint.text = "private-output"
    data = model_input()
    if V2:
        data = [msg("private-input")]
    else:
        data[0]["content"] = "private-input"
    await make_model(endpoint)(data)
    attrs = str(dict(spans_by_kind(exporter, "LLM")[0].attributes))
    if "input" in flag:
        assert "private-input" not in attrs
    if "output" in flag:
        assert "private-output" not in attrs
    assert instrumentor._enabled


async def test_privacy_credentials_media_and_signed_url(
    transport_tracing, endpoint, monkeypatch
):
    instrumentor, provider, exporter = transport_tracing
    monkeypatch.setenv("FI_API_KEY", "local-fi-secret-12345")
    # S5 seeds environment values at initialization, never during span capture.
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    endpoint.text = (
        API_KEY
        + " local-fi-secret-12345 https://example.test/a?sig=signed-secret data:image/png;base64,QUJD"
    )
    await make_model(endpoint)(model_input())
    attrs = str(dict(spans_by_kind(exporter, "LLM")[0].attributes))
    for value in (API_KEY, "local-fi-secret-12345", "signed-secret", "QUJD"):
        assert value not in attrs


async def test_echoed_provider_secret_redacted_from_agent_and_chain(
    transport_tracing, endpoint
):
    _, _, exporter = transport_tracing
    endpoint.text = "echoed: " + API_KEY
    agent = make_agent(endpoint)
    if V2:
        stream = pipeline.TeamPipeline(leader=agent, members=[]).reply_stream(msg())
        _ = [item async for item in stream]
    else:
        await pipeline.SequentialPipeline([agent])(msg())
    (agent_span,) = spans_by_kind(exporter, "AGENT")
    assert agent_span.attributes["output.value"].startswith("echoed: ")
    assert len(spans_by_kind(exporter, "CHAIN")) == 1
    assert all(
        API_KEY not in str(dict(s.attributes)) for s in exporter.get_finished_spans()
    )


async def test_wire_dedup_survives_native_span(tracing, endpoint):
    import httpx
    from fi_instrumentation import FITracer
    from fi_instrumentation.auto import wire

    instrumentor, provider, exporter = tracing
    wire.enable(FITracer(provider.get_tracer("wire-test")), transports=["httpx"])
    native = provider.get_tracer("native-sdk")

    class NativeModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            with native.start_as_current_span("native-http-client"):
                async with httpx.AsyncClient() as client:
                    result = await client.post(
                        endpoint.url + "/chat/completions",
                        json={
                            "model": "local-test",
                            "messages": [{"role": "user", "content": "question"}],
                        },
                    )
                    assert result.status_code == 200
            return response()

    try:
        instance = NativeModel.__new__(NativeModel)
        await instance([])
    finally:
        wire.disable()
    spans = exporter.get_finished_spans()
    assert len(spans_by_kind(exporter, "LLM")) == 1
    assert not any(s.attributes.get("traceai.capture.layer") == "wire" for s in spans)
    assert len(spans) == 2
    assert instrumentor._enabled


@pytest.mark.skipif(not V2, reason="2.x explicit tool result state")
async def test_tool_error_result_marks_error_without_raising(tracing):
    _, _, exporter = tracing
    chunks = [chunk async for chunk in call_tool(make_toolkit(), name="not-a-tool")]
    assert chunks
    (span,) = spans_by_kind(exporter, "TOOL")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "agentscope.tool.ToolError"


async def test_agent_error_propagates_and_marks_error(tracing, endpoint):
    import openai

    _, _, exporter = tracing
    endpoint.status = 400
    with pytest.raises(openai.BadRequestError):
        await call_agent(make_agent(endpoint))
    (span,) = spans_by_kind(exporter, "AGENT")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"].endswith("BadRequestError")


@pytest.mark.parametrize("hidden", [False, True])
async def test_invocation_parameters_masking_and_zero_values(retrace, hidden):
    _, _, exporter = retrace(hide_llm_invocation_parameters=hidden)

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            return response()

    instance = CustomModel.__new__(CustomModel)
    await instance([], temperature=0.0, seed=0, password="not-recorded")
    (span,) = spans_by_kind(exporter, "LLM")
    attrs = dict(span.attributes)
    if hidden:
        assert not any(key.startswith("gen_ai.request.") for key in attrs)
    else:
        assert attrs["gen_ai.request.temperature"] == 0.0
        assert attrs["gen_ai.request.seed"] == 0
        assert json.loads(attrs["gen_ai.request.parameters"]) == {
            "temperature": 0.0,
            "seed": 0,
        }
    assert "not-recorded" not in str(attrs)


def test_entry_point_packaging():
    entry_points = distribution("traceAI-agentscope").entry_points
    assert any(
        e.group == "opentelemetry_instrumentor"
        and e.name == "agentscope"
        and e.load().__name__ == "AgentScopeInstrumentor"
        for e in entry_points
    )


def test_bounded_serialization_does_not_call_user_code(tracing):
    instrumentor, _, _ = tracing

    class Hostile:
        def __str__(self):
            raise AssertionError("must not stringify")

        @property
        def content(self):
            raise AssertionError("must not invoke getters")

    cycle = {}
    cycle["cycle"] = cycle
    value = instrumentor._attributes.json(
        {"value": Hostile(), "cycle": cycle, "nan": float("nan")}
    )
    assert "Hostile" in value
    assert "capture limit" in value
    assert json.loads(value)["nan"] == "NaN"
    assert len(value) < 10_000
