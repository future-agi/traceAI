"""Offline regressions: hide flags on embedding text, streamed chat at the
declared core floor, and cancelled workflow steps.

Everything runs on LlamaIndex's real dispatcher with MockLLM, MockEmbedding and
the real OpenAI LLM over an in-process HTTP transport. Spans go through the instrumentor's FITracer, so TraceConfig masking applies
exactly as it does on the register() path.
"""

import asyncio
import json
import logging

import pytest
from fi_instrumentation import TraceConfig
from llama_index.core.base.llms.types import ChatMessage, MessageRole
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.llms import MockLLM
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_llamaindex import LlamaIndexInstrumentor


QUERY = "synthetic-private-query"
DOCUMENT = "synthetic-private-document"
HANDLER_LOGGER = "traceai_llamaindex._handler"


@pytest.fixture
def trace(monkeypatch):
    # The hide flags must come only from the TraceConfig under test.
    for name in (
        "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT",
        "FI_HIDE_EMBEDDING_VECTORS",
    ):
        monkeypatch.delenv(name, raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()

    def start(config=None):
        instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
        return exporter

    try:
        yield start
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()


def _embed():
    model = MockEmbedding(embed_dim=3)
    assert model.get_query_embedding(QUERY) == [0.5, 0.5, 0.5]
    assert model.get_text_embedding_batch([DOCUMENT]) == [[0.5, 0.5, 0.5]]


def _embedding_spans(exporter):
    # The public entry points; the private _get_*_embedding spans they open
    # carry no embedding attributes.
    spans = [
        span for span in exporter.get_finished_spans()
        if span.name.endswith((".get_query_embedding", ".get_text_embedding_batch"))
    ]
    assert len(spans) == 2, [s.name for s in exporter.get_finished_spans()]
    assert all(s.attributes["gen_ai.span.kind"] == "EMBEDDING" for s in spans)
    return spans


def test_embedding_text_is_recorded_by_default(trace):
    exporter = trace()
    _embed()
    texts = {
        span.attributes["embedding.embeddings.0.embedding.text"]
        for span in _embedding_spans(exporter)
    }
    assert texts == {QUERY, DOCUMENT}
    for span in _embedding_spans(exporter):
        assert tuple(span.attributes["embedding.embeddings.0.embedding.vector"]) == (0.5, 0.5, 0.5)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
def test_input_hide_flags_hide_embedding_text(trace, flag):
    exporter = trace(TraceConfig(**{flag: True}))
    _embed()
    for span in _embedding_spans(exporter):
        attrs = dict(span.attributes)
        # Every flag that the docs say hides embedding input text keeps it out of
        # the embedding text key and of the embedding span's output.
        assert QUERY not in str(attrs.get("embedding.embeddings.0.embedding.text", ""))
        assert DOCUMENT not in str(attrs.get("embedding.embeddings.0.embedding.text", ""))
        assert QUERY not in str(attrs.get("output.value", ""))
        assert DOCUMENT not in str(attrs.get("output.value", ""))
        # The vector itself is not input text and stays visible.
        assert tuple(attrs["embedding.embeddings.0.embedding.vector"]) == (0.5, 0.5, 0.5)
    if flag == "hide_inputs":
        # FI_HIDE_INPUTS alone (no FI_HIDE_OUTPUTS) keeps the text out of every span.
        for span in exporter.get_finished_spans():
            assert QUERY not in str(dict(span.attributes))
            assert DOCUMENT not in str(dict(span.attributes))


def test_hide_embedding_vectors_hides_every_vector_copy(trace):
    exporter = trace(TraceConfig(hide_embedding_vectors=True))
    _embed()
    for span in _embedding_spans(exporter):
        attrs = dict(span.attributes)
        assert "embedding.embeddings.0.embedding.vector" not in attrs
        assert "0.5" not in str(attrs.get("output.value", ""))
        assert "0.5" not in str(attrs.get("embedding.embeddings", ""))
        assert attrs["embedding.embeddings.0.embedding.text"] in (QUERY, DOCUMENT)


def _sse(*parts):
    events = []
    for index, part in enumerate(parts):
        chunk = {
            "id": "synthetic-stream", "object": "chat.completion.chunk", "created": 1,
            "model": "gpt-4o-mini",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": part} if index == 0 else {"content": part},
                "finish_reason": "stop" if index == len(parts) - 1 else None,
            }],
        }
        events.append(f"data: {json.dumps(chunk)}\n\n")
    events.append("data: [DONE]\n\n")
    return "".join(events).encode()


@pytest.fixture
def offline_openai(monkeypatch):
    """The real llama-index OpenAI LLM over an in-process HTTP transport."""
    openai_base = pytest.importorskip("llama_index.llms.openai.base")
    httpx = pytest.importorskip("httpx")

    def respond(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": QUERY}]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse("synthetic-", "answer"),
        )

    real_client = openai_base.SyncOpenAI
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        def client(**kwargs):
            kwargs["http_client"] = http_client
            return real_client(**kwargs)

        monkeypatch.setattr(openai_base, "SyncOpenAI", client)
        yield openai_base.OpenAI(
            model="gpt-4o-mini", api_key="synthetic-local-only",
            api_base="https://offline.invalid/v1", max_retries=0,
        )


def _stream_chat_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name == "OpenAI.stream_chat"]


def test_stream_chat_span_ends_when_the_stream_finishes(trace, offline_openai, caplog):
    exporter = trace()
    caplog.set_level(logging.WARNING, logger=HANDLER_LOGGER)
    stream = offline_openai.stream_chat([ChatMessage(role=MessageRole.USER, content=QUERY)])
    assert not _stream_chat_spans(exporter)  # still streaming
    chunks = list(stream)
    assert chunks[-1].message.content == "synthetic-answer"
    # No 60 s sweeper wait and no shutdown: the span is exported as the stream ends.
    span, = _stream_chat_spans(exporter)
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["gen_ai.span.kind"] == "LLM"
    assert span.attributes["gen_ai.input.messages.0.message.content"] == QUERY
    assert span.attributes["gen_ai.output.messages.0.message.content"] == "synthetic-answer"
    errors = [r for r in caplog.records if r.name == HANDLER_LOGGER and r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


def test_a_failing_event_handler_does_not_strand_a_streaming_span(trace, offline_openai, monkeypatch):
    from traceai_llamaindex import _handler

    exporter = trace()

    def broken(self, *args, **kwargs):
        raise RuntimeError("synthetic handler failure")

    monkeypatch.setattr(_handler._Span, "_process_messages", broken)
    chunks = list(offline_openai.stream_chat([ChatMessage(role=MessageRole.USER, content=QUERY)]))
    assert chunks[-1].message.content == "synthetic-answer"
    span, = _stream_chat_spans(exporter)
    assert span.attributes["gen_ai.span.kind"] == "LLM"


def test_chat_at_any_core_logs_no_handler_errors(trace, caplog):
    exporter = trace()
    caplog.set_level(logging.WARNING, logger=HANDLER_LOGGER)
    messages = [ChatMessage(role=MessageRole.USER, content=QUERY)]
    response = MockLLM(max_tokens=2).chat(messages)
    assert response.message.content == "text text"
    chat = [s for s in exporter.get_finished_spans() if s.name.endswith(".chat")]
    assert chat
    assert any(QUERY in str(s.attributes.get("input.value", "")) for s in chat)
    errors = [r for r in caplog.records if r.name == HANDLER_LOGGER and r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


def _workflow_classes():
    from traceai_llamaindex import _handler

    if _handler.SpanCancelledEvent is None:
        pytest.skip("SpanCancelledEvent is emitted from llama-index-workflows 2")
    from workflows import Workflow, step
    from workflows.events import StartEvent, StopEvent

    class Slow(Workflow):
        @step
        async def s(self, ev: StartEvent) -> StopEvent:
            await asyncio.sleep(5)
            return StopEvent(result="never")

    return Slow


def _by_short_name(exporter):
    # The class is local to a function, so span names carry "<locals>." prefixes.
    spans = {}
    for span in exporter.get_finished_spans():
        short = ".".join(span.name.split(".")[-2:])
        assert short not in spans, short
        spans[short] = span
    return spans


def _cancel_event(span):
    return [e for e in span.events if e.name == "span.cancelled"]


def test_timed_out_step_is_marked_cancelled_not_ok(trace):
    Slow = _workflow_classes()
    exporter = trace()

    async def run():
        await Slow(timeout=0.3).run()

    with pytest.raises(Exception, match="timed out"):
        asyncio.run(run())
    spans = _by_short_name(exporter)
    step_span, run_span = spans["Slow.s"], spans["Slow.run"]
    assert step_span.parent.span_id == run_span.context.span_id
    assert run_span.status.status_code == StatusCode.ERROR
    assert step_span.status.status_code != StatusCode.OK
    event, = _cancel_event(step_span)
    assert event.attributes["reason"] == "step cancelled"
    assert step_span.attributes["gen_ai.span.kind"] == "CHAIN"


def test_user_cancelled_run_is_marked_cancelled_not_ok(trace):
    Slow = _workflow_classes()
    exporter = trace()

    async def run():
        handler = Slow(timeout=10).run()
        await asyncio.sleep(0.2)
        await handler.cancel_run()
        try:
            await handler
        except Exception:
            pass

    asyncio.run(run())
    spans = _by_short_name(exporter)
    for name in ("Slow.s", "Slow.run"):
        assert spans[name].status.status_code != StatusCode.OK, name
        assert spans[name].status.status_code != StatusCode.ERROR, name
        assert _cancel_event(spans[name]), name
