"""Real SDK migration contracts; synthetic HTTP and localhost OTLP only."""

import asyncio
import re
from pathlib import Path

import openai
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_openai import OpenAIInstrumentor, set_client_provider

from .test_ollama_provider import (
    FAKE_KEY,
    MODEL,
    PRIVATE_INPUT,
    PRIVATE_OUTPUT,
    assert_response,
    attributes,
    call,
    collector,  # noqa: F401 -- shared localhost receiver fixture
    handler_for,
    httpx,
    make_client,
    spans_on_wire,
)

VLLMInstrumentor = pytest.importorskip("traceai_vllm").VLLMInstrumentor
BASE_URL = "http://localhost:8000/v1"


@pytest.fixture
def telemetry(collector):  # noqa: F811 -- pytest resolves the imported fixture
    port, bodies = collector
    bodies.clear()
    session = requests.Session()
    session.trust_env = False
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{port}/v1/traces", session=session
            )
        )
    )
    yield provider, bodies
    for instrumentor in (VLLMInstrumentor(), OpenAIInstrumentor()):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    provider.shutdown()
    session.close()


def invoke(operation, use_async):
    async def run_async():
        async with make_client(BASE_URL, operation, True) as client:
            set_client_provider(client, provider="vllm")
            result = await call(client, operation)
            if operation.endswith("-stream"):
                assert [chunk async for chunk in result]
            else:
                assert_response(result, operation)

    def run_sync():
        with make_client(BASE_URL, operation) as client:
            set_client_provider(client, provider="vllm")
            result = call(client, operation)
            if operation.endswith("-stream"):
                assert list(result)
            else:
                assert_response(result, operation)

    if operation.startswith("error"):
        with pytest.raises(openai.AuthenticationError):
            asyncio.run(run_async()) if use_async else run_sync()
    else:
        asyncio.run(run_async()) if use_async else run_sync()


@pytest.mark.parametrize("first", ["shared", "legacy"])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "operation",
    [
        "chat",
        "chat-stream",
        "embeddings",
        "responses",
        "responses-stream",
        "error-chat",
        "error-embeddings",
        "error-responses",
    ],
)
def test_shared_owns_one_span(telemetry, first, use_async, private, operation):
    provider, bodies = telemetry
    shared, legacy = OpenAIInstrumentor(), VLLMInstrumentor()
    order = [shared, legacy] if first == "shared" else [legacy, shared]
    # Different providers/configs prove the shared route owns privacy and export.
    unused_provider = TracerProvider()
    legacy_exporter = InMemorySpanExporter()
    unused_provider.add_span_processor(SimpleSpanProcessor(legacy_exporter))
    try:
        for inst in order:
            inst.instrument(
                tracer_provider=provider if inst is shared else unused_provider,
                config=TraceConfig(
                    hide_inputs=private if inst is shared else False,
                    hide_outputs=private if inst is shared else False,
                    hide_embedding_vectors=private,
                    hide_llm_invocation_parameters=private,
                    hide_error_details=private,
                ),
            )
        invoke(operation, use_async)
        spans = spans_on_wire(bodies)
        assert len(spans) == 1
        assert not legacy_exporter.get_finished_spans()
        attrs = attributes(spans[0])
        assert attrs["gen_ai.provider.name"] == "vllm"
        assert attrs["gen_ai.request.model"] == MODEL
        assert spans[0].status.code == (2 if operation.startswith("error") else 1)
        payload = str(spans)
        assert FAKE_KEY not in payload
        if private:
            assert PRIVATE_INPUT not in payload
            assert PRIVATE_OUTPUT not in payload
    finally:
        unused_provider.shutdown()


@pytest.mark.parametrize("first", ["shared", "legacy"])
@pytest.mark.parametrize("remove", ["shared", "legacy"])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("operation", ["chat", "chat-stream", "error-chat"])
def test_independent_teardown_and_reenable(
    telemetry, first, remove, use_async, operation
):
    provider, bodies = telemetry
    instruments = {"shared": OpenAIInstrumentor(), "legacy": VLLMInstrumentor()}
    order = [first, "legacy" if first == "shared" else "shared"]
    for name in order:
        instruments[name].instrument(tracer_provider=provider)
    invoke(operation, use_async)
    assert len(spans_on_wire(bodies)) == 1
    bodies.clear()
    instruments[remove].uninstrument()
    invoke(operation, use_async)
    spans = spans_on_wire(bodies)
    assert len(spans) == 1
    assert attributes(spans[0])["gen_ai.provider.name"] == "vllm"
    bodies.clear()
    instruments[remove].instrument(tracer_provider=provider)
    invoke(operation, use_async)
    assert len(spans_on_wire(bodies)) == 1
    for name in reversed(order):
        instruments[name].uninstrument()
    bodies.clear()
    invoke(operation, use_async)
    assert spans_on_wire(bodies) == []


@pytest.mark.parametrize("package", ["openai", "vllm"])
def test_readme_quickstart(telemetry, monkeypatch, package):
    import fi_instrumentation

    provider, bodies = telemetry
    original_init = openai.OpenAI.__init__

    def mock_transport(self, *args, **kwargs):
        kwargs["http_client"] = httpx.Client(
            transport=httpx.MockTransport(handler_for("chat")), trust_env=False
        )
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(openai.OpenAI, "__init__", mock_transport)
    monkeypatch.setattr(fi_instrumentation, "register", lambda **kwargs: provider)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("VLLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("VLLM_MODEL", MODEL)
    text = (Path(__file__).parents[2] / package / "README.md").read_text()
    if package == "openai":
        text = text.split("## Quickstart", 1)[1]
    snippets = re.findall(r"```python\n(.*?)\n```", text, re.S)
    namespace = {}
    for snippet in snippets[:3] if package == "openai" else snippets[:1]:
        exec(compile(snippet, f"{package}-README", "exec"), namespace)
    spans = spans_on_wire(bodies)
    assert len(spans) == 1
    assert attributes(spans[0])["gen_ai.provider.name"] == (
        "openai" if package == "openai" else "vllm"
    )
    assert FAKE_KEY not in str(spans)


def test_legacy_warns_without_removing_entrypoint(telemetry):
    provider, _ = telemetry
    with pytest.warns(DeprecationWarning, match="OpenAIInstrumentor"):
        VLLMInstrumentor().instrument(tracer_provider=provider)
