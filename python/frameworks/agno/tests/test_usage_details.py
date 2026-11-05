"""Preserve billing inputs through real Agno clients and all model wrapper paths."""

from types import SimpleNamespace

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from openinference.instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_agno import configure_agno_tracing
from traceai_agno._wrappers._model_wrapper import _ModelWrapper

from . import test_release_privacy_workflows as loopback

model = loopback.model
server = loopback.server

# Assert literal wire keys independently of the implementation's mapping.
USAGE_ATTRIBUTES = {
    "input_tokens": "llm.token_count.prompt",
    "output_tokens": "llm.token_count.completion",
    "total_tokens": "llm.token_count.total",
    "cache_read_tokens": "llm.token_count.prompt_details.cache_read",
    "cache_write_tokens": "llm.token_count.prompt_details.cache_write",
    "audio_input_tokens": "llm.token_count.prompt_details.audio",
    "audio_output_tokens": "llm.token_count.completion_details.audio",
    "reasoning_tokens": "llm.token_count.completion_details.reasoning",
    "cost": "llm.cost.total",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("zero_details", [False, True])
@pytest.mark.parametrize("hide_content", [False, True])
async def test_loopback_usage_details_reach_otlp(
    model,
    server,
    tracing,
    receiver,
    asynchronous,
    streaming,
    zero_details,
    hide_content,
):
    audio_input, audio_output, reasoning, cached = (
        (0, 0, 0, 0) if zero_details else (7, 11, 9, 4)
    )
    server["usage"] = {
        "prompt_tokens": 30,
        "completion_tokens": 20,
        "total_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": cached, "audio_tokens": audio_input},
        "completion_tokens_details": {
            "reasoning_tokens": reasoning,
            "audio_tokens": audio_output,
        },
    }
    provider, exporter = tracing
    endpoint, requests = receiver
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    instrumentation = configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hide_content, hide_outputs=hide_content),
    )
    try:
        agent = Agent(model=model("gpt-4o-audio-preview"), telemetry=False)
        if asynchronous:
            if streaming:
                events = [
                    event
                    async for event in agent.arun(
                        loopback.INPUT, stream=True, stream_events=True
                    )
                ]
                result = events[-1]
            else:
                result = await agent.arun(loopback.INPUT)
        elif streaming:
            events = list(agent.run(loopback.INPUT, stream=True, stream_events=True))
            result = events[-1]
        else:
            result = agent.run(loopback.INPUT)

        assert result.content == loopback.OUTPUT
        expected = {
            "input_tokens": 30,
            "output_tokens": 20,
            "total_tokens": 50,
            "cache_read_tokens": cached,
            "audio_input_tokens": audio_input,
            "audio_output_tokens": audio_output,
            "reasoning_tokens": reasoning,
        }
        # First prove the real OpenAI client and Agno retained the billing inputs.
        for field, value in expected.items():
            assert getattr(result.metrics, field) == value
        spans = exporter.get_finished_spans()
        llms = [
            s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"
        ]
        assert len(llms) == 1
        attributes = dict(llms[0].attributes)
        for field, value in expected.items():
            assert attributes.get(USAGE_ATTRIBUTES[field]) == value
        # No pricing table is invented when the provider does not supply a cost.
        assert "llm.cost.total" not in attributes
        assert len(server["requests"]) == 1
        assert provider.force_flush()

        exported = []
        for _, _, payload in requests:
            message = ExportTraceServiceRequest.FromString(payload)
            for resource in message.resource_spans:
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        attrs = {
                            item.key: getattr(
                                item.value, item.value.WhichOneof("value")
                            )
                            for item in span.attributes
                        }
                        if attrs.get("openinference.span.kind") == "LLM":
                            exported.append(attrs)
        assert len(exported) == 1
        for field, value in expected.items():
            assert exported[0].get(USAGE_ATTRIBUTES[field]) == value
        assert "llm.cost.total" not in exported[0]
        encoded = str(exported[0])
        assert (loopback.INPUT in encoded) is not hide_content
        assert (loopback.OUTPUT in encoded) is not hide_content
    finally:
        instrumentation.uninstrument()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("usage_kind", ["full", "zero", "partial", "missing"])
async def test_model_usage_mapping_handles_cost_and_absent_fields(
    tracing, asynchronous, streaming, usage_kind
):
    provider, exporter = tracing
    wrapper = _ModelWrapper(provider.get_tracer("usage-test"))
    model = SimpleNamespace(id="gpt-4o", name="usage-model", provider="OpenAI")
    if usage_kind == "full":
        values = dict(
            input_tokens=30,
            output_tokens=20,
            total_tokens=50,
            cache_read_tokens=4,
            cache_write_tokens=3,
            audio_input_tokens=7,
            audio_output_tokens=11,
            reasoning_tokens=9,
            cost=0.0123,
        )
    elif usage_kind == "zero":
        values = {field: 0 for field in USAGE_ATTRIBUTES}
    elif usage_kind == "partial":
        values = {"input_tokens": 30, "output_tokens": None, "cost": None}
    else:
        values = {}
    metrics = SimpleNamespace(**values) if usage_kind != "missing" else None
    response = ModelResponse(role="assistant", content="answer", response_usage=metrics)
    # Some providers finish with an empty event after their usage event.
    trailing = ModelResponse(content="", response_usage=None)

    def invoke(messages):
        return response

    async def ainvoke(messages):
        return response

    def invoke_stream(messages):
        yield response
        yield trailing

    async def ainvoke_stream(messages):
        yield response
        yield trailing

    if asynchronous:
        if streaming:
            assert [
                chunk
                async for chunk in wrapper.arun_stream(ainvoke_stream, model, ([],), {})
            ] == [response, trailing]
        else:
            assert await wrapper.arun(ainvoke, model, ([],), {}) is response
    elif streaming:
        assert list(wrapper.run_stream(invoke_stream, model, ([],), {})) == [
            response,
            trailing,
        ]
    else:
        assert wrapper.run(invoke, model, ([],), {}) is response

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    expected = {
        USAGE_ATTRIBUTES[field]: value
        for field, value in values.items()
        if value is not None
    }
    assert {
        key: attributes[key] for key in USAGE_ATTRIBUTES.values() if key in attributes
    } == expected
    if metrics is not None:
        assert vars(metrics) == values
