"""Installed-package contracts, also run against the declared published core floor."""

import subprocess
import sys

import pytest


def test_import_does_not_require_loading_openai():
    # A clean process catches module-level semantic-convention lookups; do not
    # let prior tests or the fixture's SDK imports hide an import-time failure.
    result = subprocess.run(
        [sys.executable, "-I", "-c", """
import sys

class BlockOpenAI:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "openai" or fullname.startswith("openai."):
            raise ImportError("OpenAI must not be imported during package import")

sys.meta_path.insert(0, BlockOpenAI())
from traceai_fireworks import FireworksInstrumentor
assert callable(FireworksInstrumentor)
"""],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_installed_instrumentor_exports_a_request_span():
    import httpx
    from openai import OpenAI
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode
    from traceai_fireworks import FireworksInstrumentor

    model = "accounts/fireworks/models/synthetic-floor-test"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "floor-completion",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from the floor"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        })

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FireworksInstrumentor()
    try:
        with pytest.warns(DeprecationWarning):
            instrumentor.instrument(tracer_provider=provider)
        with OpenAI(
            api_key="synthetic-floor-key",
            base_url="https://api.fireworks.ai/inference/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client:
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "Hello"}],
            )
        assert response.choices[0].message.content == "Hello from the floor"
        assert len(requests) == 1
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.OK
        assert spans[0].attributes["gen_ai.request.model"] == model
        assert spans[0].attributes["gen_ai.usage.total_tokens"] == 8
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
