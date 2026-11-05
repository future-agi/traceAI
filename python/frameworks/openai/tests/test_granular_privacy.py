"""Granular text flags hide all copies, with real SDK requests and streams."""
import json

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_openai import OpenAIInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx


@pytest.mark.parametrize("route", ["shared", "legacy"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("flag", ["text", "messages"])
def test_every_copy_hidden(route, stream, flag):
    input_text, output_text = "PRIVATE_INPUT_SENTINEL", "PRIVATE_OUTPUT_SENTINEL"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inst = (OpenAIInstrumentor() if route == "shared" else
            pytest.importorskip("traceai_vllm").VLLMInstrumentor())

    def handle(request):
        assert json.loads(request.content)["messages"][0]["content"] == input_text
        data = {
            "id": "test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": output_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        if stream:
            data["object"] = "chat.completion.chunk"
            data["choices"][0]["delta"] = data["choices"][0].pop("message")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=data)

    try:
        inst.instrument(tracer_provider=provider, config=TraceConfig(**{
            "hide_input_" + flag: True, "hide_output_" + flag: True,
        }))
        with openai.OpenAI(api_key="synthetic-key", base_url="http://localhost:8000/v1", max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            result = client.chat.completions.create(model="test", stream=stream,
                messages=[{"role": "user", "content": input_text}])
            if stream:
                assert "".join(chunk.choices[0].delta.content for chunk in result) == output_text
            else:
                assert result.choices[0].message.content == output_text
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert input_text not in spans[0].to_json()
        assert output_text not in spans[0].to_json()
        assert spans[0].attributes["gen_ai.request.model"] == "test"
        assert spans[0].attributes["gen_ai.usage.input_tokens"] == 2
        assert spans[0].attributes["gen_ai.usage.output_tokens"] == 3
    finally:
        inst.uninstrument()
        provider.shutdown()
