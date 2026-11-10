"""Offline contracts against LlamaIndex's real dispatcher and local SDK models."""

import json

import pytest
from fi_instrumentation import TraceConfig
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.llms import MockLLM
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_llamaindex import LlamaIndexInstrumentor, get_current_span


dispatcher = get_dispatcher(__name__)


@pytest.fixture
def traces():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter, instrumentor, provider
    finally:
        instrumentor.uninstrument()
        assert all(h is not instrumentor._span_handler for h in get_dispatcher().span_handlers)
        provider.shutdown()


def test_completion_parentage_and_current_span(traces):
    exporter, _, _ = traces

    @dispatcher.span
    def application():
        assert get_current_span() is not None
        return MockLLM(max_tokens=2).complete("private question")

    result = application()
    assert result.text == "text text"
    spans = exporter.get_finished_spans()
    root, = [s for s in spans if "application" in s.name]
    llm, = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert llm.parent.span_id == root.context.span_id
    assert json.loads(llm.attributes["input.value"])["prompt"] == "private question"
    assert tuple(llm.attributes["gen_ai.prompts"]) == ("private question",)
    assert "text text" in llm.attributes["output.value"]
    assert llm.status.status_code == StatusCode.OK
    assert get_current_span() is None


@pytest.mark.asyncio
async def test_async_completion_and_embedding(traces):
    exporter, _, _ = traces
    result = await MockLLM(max_tokens=2).acomplete("question")
    assert result.text == "text text"
    vector = await MockEmbedding(embed_dim=3).aget_text_embedding("document")
    assert vector == [0.5, 0.5, 0.5]
    spans = exporter.get_finished_spans()
    assert any(s.attributes.get("gen_ai.span.kind") == "LLM" for s in spans)
    embeddings = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "EMBEDDING"]
    assert embeddings
    assert all(s.status.status_code == StatusCode.OK for s in spans)


def test_dispatcher_error_propagates(traces):
    exporter, _, _ = traces
    error = ValueError("offline failure")

    @dispatcher.span
    def application():
        raise error

    with pytest.raises(ValueError) as caught:
        application()
    assert caught.value is error
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].name == "exception"


def test_agent_classification():
    from traceai_llamaindex._handler import _agent_types, _init_span_kind
    assert _agent_types
    for agent_type in _agent_types:
        # Dispatch on actual SDK classes, including workflow agents after 0.13.
        assert _init_span_kind.dispatch(agent_type)(None) == "AGENT"


def test_privacy_preserves_completion(traces):
    exporter, instrumentor, provider = traces
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    result = MockLLM(max_tokens=2).complete("private question")
    assert result.text == "text text"
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        attrs = dict(span.attributes)
        assert "private question" not in str(attrs)
        assert "text text" not in str(attrs)
