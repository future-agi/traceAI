"""Legacy dependency boundary and the documented Google Gen AI migration route."""

import importlib.metadata
import json
import re
import runpy
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from traceai_vertexai import VertexAIInstrumentor

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "version,supported",
    [("1.48.0", False), ("1.49.0", True), ("1.101.0", True), ("1.102.0", False), ("2.0.0", False)],
)
def test_legacy_dependency_boundary(version, supported):
    runtime = Requirement(next(iter(VertexAIInstrumentor().instrumentation_dependencies())))
    published = next(
        Requirement(value)
        for value in importlib.metadata.requires("traceAI-vertexai")
        if Requirement(value).name == "google-cloud-aiplatform"
    )
    assert runtime.specifier == published.specifier
    assert (version in runtime.specifier) is supported


def test_legacy_sdk_keeps_the_documented_import():
    from vertexai.generative_models import GenerativeModel

    assert callable(GenerativeModel)


def test_recommended_example_matches_the_readme():
    blocks = re.findall(r"```python\n(.*?)```", (PACKAGE / "README.md").read_text(), re.S)
    example = (PACKAGE / "examples/google_genai_vertex.py").read_text()
    assert blocks[0] == example
    compile(example, "google_genai_vertex.py", "exec")
    for example_file in (PACKAGE / "examples").glob("*.py"):
        compile(example_file.read_text(), example_file.name, "exec")


def test_recommended_vertex_example_exports_a_generation_span(monkeypatch, capsys):
    # google-genai is a separate migration dependency, not a legacy runtime dependency.
    genai = pytest.importorskip("google.genai")
    pytest.importorskip("traceai_google_genai")
    import httpx
    import fi_instrumentation
    from google.oauth2.credentials import Credentials
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from traceai_google_genai import GoogleGenAIInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(fi_instrumentation, "register", lambda **kwargs: provider)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("GOOGLE_GENAI_MODEL", "test-model")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "aiplatform.googleapis.com"
        assert "/projects/example-project/locations/global/" in request.url.path
        assert request.url.path.endswith("/models/test-model:generateContent")
        assert "mountain trail" in json.loads(request.content)["contents"][0]["parts"][0]["text"]
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Mountain light."}]},
                                "finishReason": "STOP"}],
                "modelVersion": "test-model",
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3,
                                  "totalTokenCount": 13},
            },
        )

    original_client = genai.Client
    clients = []
    transports = []

    def client_with_mock_transport(**kwargs):
        assert kwargs["vertexai"] is True
        client = original_client(**kwargs, credentials=Credentials(token="synthetic-test-token"))
        original_http_client = client._api_client._httpx_client
        original_http_client.close()
        transport = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        client._api_client._httpx_client = transport
        clients.append(client)
        transports.append(transport)
        return client

    monkeypatch.setattr(genai, "Client", client_with_mock_transport)
    instrumentor = GoogleGenAIInstrumentor()
    try:
        runpy.run_path(str(PACKAGE / "examples/google_genai_vertex.py"), run_name="__main__")
        assert len(requests) == 1
        assert "Mountain light." in capsys.readouterr().out
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = dict(spans[0].attributes)
        assert attributes["gen_ai.provider.name"] == "google"
        assert attributes["gen_ai.request.model"] == "test-model"
        assert attributes["gen_ai.usage.input_tokens"] == 10
        assert attributes["gen_ai.usage.output_tokens"] == 3
        assert "Mountain light." in attributes["output.value"]
    finally:
        instrumentor.uninstrument()
        for transport in transports:
            transport.close()
        provider.shutdown()
