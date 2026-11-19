"""Whole-response output must not carry request prompt text that input settings hide."""
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_vllm import VLLMInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx

PROMPT_SENTINEL = "SENTINEL_RENDERED_PROMPT_9054"
REPLY = "synthetic generated reply"


def _run_chat(config):
    def respond(request):
        # vLLM ChatCompletionResponse.prompt_text is the rendered request prompt.
        return httpx.Response(200, json={
            "id": "c1",
            "object": "chat.completion",
            "created": 1,
            "model": "test-vllm-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": REPLY}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            "prompt_text": PROMPT_SENTINEL,
        })

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = VLLMInstrumentor()
    with pytest.warns(DeprecationWarning):
        instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        with openai.OpenAI(
            base_url="http://localhost:8000/v1", api_key="synthetic-not-a-real-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False),
        ) as client:
            response = client.chat.completions.create(
                model="test-vllm-model",
                messages=[{"role": "user", "content": "synthetic question"}],
                extra_body={"return_prompt_text": True},
            )
        assert response.model_dump()["prompt_text"] == PROMPT_SENTINEL
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return json.dumps(dict(spans[0].attributes), default=str) + json.dumps(
            [(e.name, dict(e.attributes or {})) for e in spans[0].events], default=str
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    assert REPLY in _run_chat(TraceConfig())


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}, {"hide_input_images": True}],
)
def test_rawOutputOmitsRenderedPromptUnderInputSettings(trace_config):
    assert PROMPT_SENTINEL not in _run_chat(TraceConfig(**trace_config))
