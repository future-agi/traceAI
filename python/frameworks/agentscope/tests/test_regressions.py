"""Verifier regressions, using real AgentScope and S5 transport observation."""

import asyncio
from collections import UserDict
from types import MappingProxyType

import httpx
import pytest
from agentscope import model, pipeline
from fi_instrumentation import FITracer
from fi_instrumentation.auto import wire
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import StatusCode
from traceai_agentscope._attributes import MAX_TEXT, OMITTED_TEXT

from .conftest import API_KEY, V2, make_agent, make_model, model_input, msg
from .test_instrumentor import spans_by_kind


@pytest.mark.parametrize(
    "headers_type",
    [dict, MappingProxyType, httpx.Headers, UserDict],
    ids=["dict", "mappingproxy", "httpx", "userdict"],
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "x-api-key",
        "api-key",
        "xi-api-key",
        "x-goog-api-key",
        "anthropic-api-key",
        "Ocp-Apim-Subscription-Key",
    ],
)
async def test_echoed_custom_auth_headers_redacted_by_transport_registry(
    transport_tracing, endpoint, header, stream, headers_type
):
    _, _, exporter = transport_tracing
    secret = "local-header-secret-12345678"
    value = "Bearer " + secret if header == "Authorization" else secret
    endpoint.echo_header = header
    agent = make_agent(endpoint, stream=stream)
    headers = headers_type({header: value})
    # Unlike default_headers= on with_options(), set_default_headers preserves
    # the original container in the real SDK's _custom_headers storage.
    agent.model.client = agent.model.client.with_options(set_default_headers=headers)
    assert agent.model.client._custom_headers is headers
    if V2:
        result = [
            item
            async for item in pipeline.TeamPipeline(
                leader=agent, members=[]
            ).reply_stream(msg())
        ]
    else:
        result = await pipeline.SequentialPipeline([agent])(msg())
    assert result is not None
    assert endpoint.headers[0][header.lower()] == value
    for kind in ("LLM", "AGENT", "CHAIN"):
        (span,) = spans_by_kind(exporter, kind)
        assert secret not in str(dict(span.attributes))
        assert secret not in str((span.status.description, span.events))
    (llm,) = spans_by_kind(exporter, "LLM")
    assert llm.attributes["output.value"] == "echoed: <redacted>"


@pytest.mark.parametrize(
    "headers_type",
    [dict, MappingProxyType, httpx.Headers, UserDict],
    ids=["dict", "mappingproxy", "httpx", "userdict"],
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "x-api-key",
        "api-key",
        "xi-api-key",
        "x-goog-api-key",
        "anthropic-api-key",
        "Ocp-Apim-Subscription-Key",
    ],
)
async def test_echoed_per_call_auth_headers_redacted_by_transport_registry(
    transport_tracing, endpoint, header, stream, headers_type
):
    instrumentor, provider, exporter = transport_tracing
    secret = "local-per-call-header-secret-12345678"
    value = "Bearer " + secret if header == "Authorization" else secret
    headers = headers_type({header: value})
    endpoint.echo_header = header
    instance = make_model(endpoint, stream)
    wire.disable()
    instrumentor.uninstrument()
    baseline = await instance(model_input(), extra_headers=headers)
    if stream:
        baseline = [chunk async for chunk in baseline][-1]
    baseline_text = instrumentor._attributes.content(baseline)
    # OpenAI 1.x retains both differently cased Authorization entries for an
    # HTTPX per-call mapping. The loopback server echoes the first (SDK) value.
    # Do not "fix" that native precedence by rewriting application headers.
    expected = (
        API_KEY
        if not V2 and headers_type is httpx.Headers and header == "Authorization"
        else secret
    )
    assert baseline_text == "echoed: " + expected
    instrumentor.instrument(tracer_provider=provider)
    wire.enable(FITracer(provider.get_tracer("per-call-regression")), transports=["httpx", "httpx2"])
    result = await instance(model_input(), extra_headers=headers)
    if stream:
        result = [chunk async for chunk in result][-1]
    assert instrumentor._attributes.content(result) == baseline_text
    assert endpoint.headers[0] == endpoint.headers[1]
    assert endpoint.headers[1][header.lower()] == value
    assert dict(headers) == {
        header.lower() if headers_type is httpx.Headers else header: value
    }
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.attributes["output.value"] == "echoed: <redacted>"
    assert secret not in str(dict(span.attributes))
    assert secret not in str((span.status.description, span.events))


@pytest.mark.parametrize("stream", [False, True])
async def test_transport_secret_redacted_from_agent_and_chain(
    transport_tracing, endpoint, stream
):
    instrumentor, _, exporter = transport_tracing
    secret = "nested-per-call-auth-secret-12345678"
    headers = MappingProxyType({"Authorization": "Bearer " + secret})
    endpoint.echo_header = "Authorization"
    instance = make_model(endpoint, stream)
    if V2:
        from agentscope.agent import Agent, InjectionConfig
        from agentscope.middleware import MiddlewareBase

        class ForwardHeaders(MiddlewareBase):
            async def on_model_call(self, agent, input_kwargs, next_handler):
                kwargs = dict(input_kwargs)
                current_model = kwargs.pop("current_model")
                return await current_model(**kwargs, extra_headers=headers)

        agent = Agent(
            name="assistant",
            system_prompt="Be concise.",
            model=instance,
            injection_config=InjectionConfig(),
            middlewares=[ForwardHeaders()],
        )
        result = [
            item
            async for item in pipeline.TeamPipeline(
                leader=agent, members=[]
            ).reply_stream(msg())
        ]
    else:
        from agentscope.agent import AgentBase

        class ForwardAgent(AgentBase):
            async def reply(self, x):
                result = await instance(model_input(), extra_headers=headers)
                if stream:
                    result = [chunk async for chunk in result][-1]
                return msg(instrumentor._attributes.content(result), "assistant")

        result = await pipeline.SequentialPipeline([ForwardAgent()])(msg())
    assert result is not None
    assert endpoint.headers[0]["authorization"] == "Bearer " + secret
    for kind in ("LLM", "AGENT", "CHAIN"):
        (span,) = spans_by_kind(exporter, kind)
        assert secret not in str(dict(span.attributes))
        assert secret not in str((span.status.description, span.events))
    for kind in ("LLM", "AGENT"):
        (span,) = spans_by_kind(exporter, kind)
        assert span.attributes["output.value"] == "echoed: <redacted>"


@pytest.mark.skipif(V2, reason="1.x stored generate_kwargs API")
@pytest.mark.parametrize("stream", [False, True])
async def test_stored_generation_auth_headers_redacted_by_transport_registry(
    transport_tracing, endpoint, stream
):
    instrumentor, _, exporter = transport_tracing
    secret = "stored-generation-auth-secret-12345678"
    endpoint.echo_header = "Authorization"
    instance = make_model(endpoint, stream)
    instance.generate_kwargs = {
        "extra_headers": MappingProxyType({"Authorization": "Bearer " + secret})
    }
    result = await instance(model_input())
    if stream:
        result = [chunk async for chunk in result][-1]
    assert instrumentor._attributes.content(result) == "echoed: " + secret
    assert endpoint.headers[0]["authorization"] == "Bearer " + secret
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.attributes["output.value"] == "echoed: <redacted>"
    assert secret not in str(dict(span.attributes))


@pytest.mark.parametrize("stream", [False, True])
async def test_oversized_model_output_omitted_before_credential_boundary(
    tracing, endpoint, stream
):
    instrumentor, _, exporter = tracing
    endpoint.text = "x" * (MAX_TEXT - 24) + API_KEY + " tail"
    result = await make_model(endpoint, stream)(model_input())
    if stream:
        chunks = [chunk async for chunk in result]
        result = chunks[-1]
    # Capture limits do not truncate the response seen by the application.
    assert instrumentor._attributes.field(result.content[0], "text") == endpoint.text
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.attributes["output.value"] == OMITTED_TEXT
    assert API_KEY[:24] not in str(dict(span.attributes))


def test_oversized_structured_and_message_content_never_keeps_secret_prefix(tracing):
    instrumentor, _, _ = tracing
    attrs = instrumentor._attributes
    text = "x" * (MAX_TEXT - 24) + API_KEY
    projected = {
        **attrs.messages({"role": "user", "content": text}, "input"),
        **attrs.request("TOOL", None, ({"name": "test", "input": text},), {}),
        "schema": attrs.json({"description": text}),
        "blocks": attrs.content([{"text": "x" * (MAX_TEXT - 24)}, {"text": API_KEY}]),
        "identity": attrs.request("LLM", None, (), {"messages": [{"role": text}]}),
    }
    assert API_KEY[:24] not in str(attrs.clean(projected))
    assert OMITTED_TEXT in str(projected)


async def test_stream_delta_accumulation_omits_instead_of_truncating_secret(tracing):
    _, _, exporter = tracing
    chunks = [
        {"content": [{"text": "x" * (MAX_TEXT - 24)}], "is_last": False},
        {"content": [{"text": API_KEY + " tail"}], "is_last": False},
    ]

    class DeltaModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            yield chunks[0]
            yield chunks[1]

    instance = DeltaModel.__new__(DeltaModel)
    instance.api_key = API_KEY
    result = [chunk async for chunk in instance([])]
    assert all(actual is expected for actual, expected in zip(result, chunks))
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.attributes["output.value"] == OMITTED_TEXT
    assert API_KEY[:24] not in str(dict(span.attributes))


@pytest.mark.parametrize("openai_first", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("wire_enabled", [False, True])
async def test_openai_coinstrumentation_one_llm_per_operation(
    tracing, endpoint, openai_first, stream, wire_enabled
):
    from traceai_openai import OpenAIInstrumentor

    instrumentor, provider, exporter = tracing
    openai_instrumentor = OpenAIInstrumentor()
    if openai_first:
        instrumentor.uninstrument()
    openai_instrumentor.instrument(tracer_provider=provider)
    if openai_first:
        instrumentor.instrument(tracer_provider=provider)
    if wire_enabled:
        wire.enable(
            FITracer(provider.get_tracer("dedup-regression")), transports=["httpx"]
        )
    try:
        instance = make_model(endpoint, stream)
        result = await instance(model_input())
        if stream:
            _ = [chunk async for chunk in result]
        (span,) = spans_by_kind(exporter, "LLM")
        assert span.name.startswith("agentscope.")
        assert span.attributes["output.value"] == "hello local"
        assert len(endpoint.requests) == 1
        assert not any(
            s.attributes.get("traceai.capture.layer") == "wire"
            for s in exporter.get_finished_spans()
        )
        assert not context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        assert not trace.get_current_span().get_span_context().is_valid
        # Suppression belongs only to the framework model operation.
        await instance.client.chat.completions.create(
            model="local-test", messages=[{"role": "user", "content": "direct SDK"}]
        )
        assert len(spans_by_kind(exporter, "LLM")) == 2
        assert any(s.name == "ChatCompletion" for s in exporter.get_finished_spans())
    finally:
        wire.disable()
        openai_instrumentor.uninstrument()


@pytest.mark.skipif(not V2, reason="2.x public agent/pipeline streaming APIs")
@pytest.mark.parametrize("chain", [False, True])
async def test_public_stream_close_finishes_all_descendants(tracing, endpoint, chain):
    from agentscope.event import TextBlockDeltaEvent

    _, provider, exporter = tracing
    started = []

    class Starts(SpanProcessor):
        def on_start(self, span, parent_context=None):
            started.append(span)

    provider.add_span_processor(Starts())
    agent = make_agent(endpoint, stream=True)
    owner = pipeline.TeamPipeline(leader=agent, members=[]) if chain else agent
    stream = owner.reply_stream(msg())
    async for event in stream:
        if isinstance(event, TextBlockDeltaEvent):
            break
    else:
        pytest.fail("real AgentScope did not yield a model text delta")
    assert len(started) == (3 if chain else 2)
    assert not exporter.get_finished_spans()
    await asyncio.create_task(stream.aclose())
    ended = exporter.get_finished_spans()
    assert {s.context.span_id for s in ended} == {s.context.span_id for s in started}
    assert all(s.status.status_code is StatusCode.OK for s in ended)
    assert all(s.attributes["traceai.stream.incomplete"] is True for s in ended)
    assert len({s.end_time for s in ended}) == 1
    (llm,) = spans_by_kind(exporter, "LLM")
    assert llm.attributes["output.value"] == "hello "
    await stream.aclose()
    assert len(exporter.get_finished_spans()) == len(ended)
    assert not trace.get_current_span().get_span_context().is_valid
