"""Execute the README with real SDKs, a fake HTTP transport and local spans."""

import json
import re
from pathlib import Path

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_llamaindex import LlamaIndexInstrumentor


# The provider adapter is an example dependency, not an instrumentor dependency.
# The dedicated Workflows CI matrix installs it on both core versions.
openai_base = pytest.importorskip("llama_index.llms.openai.base")
httpx = pytest.importorskip("httpx")


PROMPT = "Suggest a name for a community garden."
ANSWER = "The Neighborhood Patch"


def test_readme_chat(monkeypatch, capsys):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    registration = []
    requests = []

    def register(**kwargs):
        registration.append(kwargs)
        return provider

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [{"role": "user", "content": PROMPT}]
        return httpx.Response(200, json={
            "id": "synthetic-chat", "object": "chat.completion", "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": ANSWER,
            }, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        })

    monkeypatch.setattr("fi_instrumentation.register", register)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-local-only")
    monkeypatch.setenv("OPENAI_API_BASE", "https://offline.invalid/v1")
    for name in ("FI_API_KEY", "FI_SECRET_KEY"):
        monkeypatch.setenv(name, "synthetic-local-only")
    real_client = openai_base.SyncOpenAI
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        def client(**kwargs):
            kwargs["http_client"] = http_client
            return real_client(**kwargs)

        monkeypatch.setattr(openai_base, "SyncOpenAI", client)
        readme = Path(__file__).resolve().parents[1] / "README.md"
        blocks = re.findall(r"```python\n(.*?)```", readme.read_text(), re.S)
        assert blocks, "The quickstart must include runnable Python"
        namespace = {
            "__name__": "__main__",
            "FI_API_KEY": "synthetic-local-only",
            "FI_SECRET_KEY": "synthetic-local-only",
        }
        instrumentor = LlamaIndexInstrumentor()
        try:
            exec(compile("\n\n".join(blocks), str(readme), "exec"), namespace)
            assert ANSWER in capsys.readouterr().out
            assert len(registration) == len(requests) == 1
            assert registration[0]["project_name"] == "llama_index_app"
            assert registration[0]["project_type"].value == "observe"
            span, = exporter.get_finished_spans()
            assert span.name == "OpenAI.chat"
            assert span.status.status_code == StatusCode.OK
            assert span.attributes["gen_ai.span.kind"] == "LLM"
            assert span.attributes["gen_ai.usage.total_tokens"] == 8
            payload = encode_spans([span]).SerializeToString()
            assert PROMPT.encode() not in payload
            assert ANSWER.encode() not in payload
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            provider.shutdown()
