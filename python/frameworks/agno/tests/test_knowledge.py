"""Real Knowledge calls with a deterministic, optionally traced vector store."""

import asyncio
import json

import pytest
from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge
from openinference.instrumentation import TraceConfig
from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry import context, trace
from traceai_agno import configure_agno_tracing

QUERY = "synthetic-private-query"
CONTENT = "synthetic-private-document"


class VectorStore:
    name = "synthetic-vector-store"

    def __init__(self, tracer=None):
        self.tracer = tracer
        self.calls = []
        self.document = Document(id="doc-1", content=CONTENT)

    def exists(self):
        return True

    def create(self):
        pass

    def search(self, query, limit=10, filters=None, **kwargs):
        self.calls.append((query, limit, filters))
        if self.tracer:
            with self.tracer.start_as_current_span(
                "vector.search", kind=trace.SpanKind.CLIENT
            ):
                return [self.document]
        return [self.document]

    async def async_search(self, **kwargs):
        return self.search(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("store_traced", [False, True])
@pytest.mark.parametrize("privacy", ["visible", "fi", "openinference"])
async def test_real_knowledge_search(
    tracing, monkeypatch, asynchronous, store_traced, privacy
):
    provider, exporter = tracing
    if privacy == "fi":
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
        monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    config = (
        TraceConfig(hide_inputs=True, hide_outputs=True)
        if privacy == "openinference"
        else None
    )
    instrumentation = configure_agno_tracing(tracer_provider=provider, config=config)
    store = VectorStore(
        provider.get_tracer("independent-vector-instrumentation")
        if store_traced
        else None
    )
    knowledge = Knowledge(name="synthetic-knowledge", vector_db=store)
    before = trace.get_current_span().get_span_context()
    with provider.get_tracer("caller").start_as_current_span("caller") as parent:
        result = (
            (await knowledge.asearch(QUERY, max_results=1))
            if asynchronous
            else knowledge.search(QUERY, max_results=1)
        )
        assert trace.get_current_span() is parent
    assert trace.get_current_span().get_span_context() == before
    assert result == [store.document]
    assert result[0] is store.document
    assert store.calls == [(QUERY, 1, None)]
    spans = exporter.get_finished_spans()
    retrieval = [
        s for s in spans if s.attributes.get("openinference.span.kind") == "RETRIEVER"
    ]
    assert len(retrieval) == 1
    span = retrieval[0]
    assert span.name == ("Knowledge.asearch" if asynchronous else "Knowledge.search")
    assert span.kind == trace.SpanKind.INTERNAL
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.status.status_code == trace.StatusCode.OK
    assert span.end_time >= span.start_time
    assert span.attributes["gen_ai.data_source.id"] == "synthetic-knowledge"
    assert span.attributes["agno.knowledge.documents_count"] == 1
    encoded = json.dumps(dict(span.attributes))
    assert (QUERY in encoded) == (privacy == "visible")
    assert (CONTENT in encoded) == (privacy == "visible")
    children = [s for s in spans if s.name == "vector.search"]
    assert len(children) == int(
        store_traced
    )  # no duplicate or suppressed database spans
    if children:
        assert children[0].parent.span_id == span.context.span_id
    instrumentation.uninstrument()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_knowledge_respects_suppression_and_external_teardown(
    tracing, asynchronous
):
    provider, exporter = tracing
    original = (Knowledge.search, Knowledge.asearch)
    configure_agno_tracing(tracer_provider=provider)
    knowledge = Knowledge(vector_db=VectorStore())
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        result = (
            await knowledge.asearch(QUERY) if asynchronous else knowledge.search(QUERY)
        )
        assert result[0].content == CONTENT
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()
    AgnoInstrumentor().uninstrument()
    assert (Knowledge.search, Knowledge.asearch) == original
    knowledge.search(QUERY)
    assert not exporter.get_finished_spans()


@pytest.mark.asyncio
async def test_knowledge_cancellation_is_not_success(tracing):
    provider, exporter = tracing
    configure_agno_tracing(tracer_provider=provider)
    started = asyncio.Event()

    class DelayedStore(VectorStore):
        async def async_search(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    knowledge = Knowledge(vector_db=DelayedStore())
    task = asyncio.create_task(knowledge.asearch(QUERY))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert spans[0].end_time is not None


def test_partial_install_restores_knowledge(tracing, monkeypatch):
    from traceai_agno._wrappers import _install

    provider, _ = tracing
    original = (Knowledge.search, Knowledge.asearch)
    wrap = _install.wrap_function_wrapper

    def fail(target, name, wrapper):
        if target is Knowledge and name == "asearch":
            raise RuntimeError("synthetic install failure")
        return wrap(target, name, wrapper)

    monkeypatch.setattr(_install, "wrap_function_wrapper", fail)
    with pytest.raises(RuntimeError, match="synthetic install failure"):
        configure_agno_tracing(tracer_provider=provider)
    assert (Knowledge.search, Knowledge.asearch) == original
    assert not AgnoInstrumentor().is_instrumented_by_opentelemetry
