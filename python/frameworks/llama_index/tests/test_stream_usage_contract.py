"""Real provider adapters over an offline transport, at both SDK floors and current.

No provider credentials or network are used. Delays and failures occur while the
OpenAI SDK reads SSE bytes, not in a fake LlamaIndex dispatcher.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fi_instrumentation import TraceConfig
from llama_index.core.llms import ChatMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_llamaindex import LlamaIndexInstrumentor


# Provider SDKs are optional dependencies; the dedicated compatibility CI
# installs them in every row, unlike the package-only Poetry job.
OpenAI = pytest.importorskip("llama_index.llms.openai").OpenAI

USAGE = {
    "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
    "prompt_tokens_details": {"cached_tokens": 5},
    "completion_tokens_details": {"reasoning_tokens": 3},
}
PROMPT = "synthetic private prompt"
ANSWER = "synthetic answer"
MESSAGES = [ChatMessage(role="user", content=PROMPT)]


@pytest.fixture
def traces():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()

    def start(hidden=False):
        instrumentor.instrument(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
        )
        return exporter, instrumentor

    yield start
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


@pytest.fixture
def clients():
    state = {"mode": "ok", "last_byte_at": 0, "blocked": None, "requests": 0}

    def chunks():
        for text in ("synthetic ", "answer"):
            yield ("data: " + json.dumps({
                "id": "test", "object": "chat.completion.chunk", "created": 1,
                "model": "gpt-4o-mini", "choices": [{"index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None}],
            }) + "\n\n").encode()
        yield ("data: " + json.dumps({
            "id": "test", "object": "chat.completion.chunk", "created": 1,
            "model": "gpt-4o-mini", "choices": [], "usage": USAGE,
        }) + "\n\ndata: [DONE]\n\n").encode()

    class SyncBytes(httpx.SyncByteStream):
        def __iter__(self):
            for i, chunk in enumerate(chunks()):
                if i and state["mode"] == "error":
                    raise httpx.ReadError("synthetic stream failure")
                time.sleep(0.02)
                state["last_byte_at"] = time.time_ns()
                yield chunk

    class AsyncBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i, chunk in enumerate(chunks()):
                if i and state["mode"] == "error":
                    raise httpx.ReadError("synthetic stream failure")
                if i and state["mode"] == "block":
                    state["blocked"].set()
                    await asyncio.Event().wait()
                await asyncio.sleep(0.02)
                state["last_byte_at"] = time.time_ns()
                yield chunk

    def respond(request, asynchronous=False):
        state["requests"] += 1
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=AsyncBytes() if asynchronous else SyncBytes())
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1,
            "model": "gpt-4o-mini", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": ANSWER},
                "finish_reason": "stop"}], "usage": USAGE,
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as sync:
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: respond(request, True)
        ))
        yield sync, async_client, state
        asyncio.run(async_client.aclose())


def model(clients, cls=OpenAI, **kwargs):
    sync, asynchronous, _ = clients
    return cls(model="gpt-4o-mini", api_key="synthetic-not-a-key", max_retries=0,
               http_client=sync, async_http_client=asynchronous, **kwargs)


def span(exporter, suffix):
    spans = [s for s in exporter.get_finished_spans() if s.name.endswith(suffix)]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return spans[0]


def assert_cancelled(exporter, instrumentor, suffix):
    result = span(exporter, suffix)
    assert result.status.status_code == StatusCode.ERROR
    assert result.status.description == "cancelled"
    assert result.attributes["llama_index.cancelled"] is True
    assert not result.events or all(e.name != "exception" for e in result.events)
    assert not instrumentor._span_handler.open_spans
    assert not any(s.active for s in instrumentor._span_handler._export_queue.spans.values())


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("method", ["stream_chat", "stream_complete"])
def test_sync_close_is_immediate(traces, clients, started, method):
    # Include models constructed before instrumentation is enabled.
    llm = model(clients)
    exporter, instrumentor = traces()
    stream = getattr(llm, method)(MESSAGES if method.endswith("chat") else PROMPT)
    if started:
        next(stream)
    stream.close()
    assert_cancelled(exporter, instrumentor, "." + method)
    stream.close()
    assert len([s for s in exporter.get_finished_spans() if s.name.endswith("." + method)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("method", ["astream_chat", "astream_complete"])
async def test_async_close_is_immediate(traces, clients, started, method):
    exporter, instrumentor = traces()
    stream = await getattr(model(clients), method)(MESSAGES if method.endswith("chat") else PROMPT)
    if started:
        await stream.__anext__()
    await stream.aclose()
    assert_cancelled(exporter, instrumentor, "." + method)


@pytest.mark.parametrize("method", ["stream_chat", "stream_complete"])
def test_sync_duration_includes_consumption(traces, clients, method):
    exporter, _ = traces()
    stream = getattr(model(clients), method)(MESSAGES if method.endswith("chat") else PROMPT)
    chunks = list(stream)
    assert chunks
    result = span(exporter, "." + method)
    assert result.status.status_code == StatusCode.OK
    assert result.end_time >= clients[2]["last_byte_at"]
    assert result.events
    assert all(result.start_time <= e.timestamp <= result.end_time for e in result.events)


@pytest.mark.asyncio
async def test_async_duration_includes_consumption(traces, clients):
    exporter, _ = traces()
    stream = await model(clients).astream_chat(MESSAGES)
    assert [chunk async for chunk in stream]
    result = span(exporter, ".astream_chat")
    assert result.status.status_code == StatusCode.OK
    assert result.end_time >= clients[2]["last_byte_at"]
    assert result.events
    assert all(e.timestamp <= result.end_time for e in result.events)


def test_sync_failure_is_preserved(traces, clients):
    exporter, _ = traces()
    clients[2]["mode"] = "error"
    stream = model(clients).stream_chat(MESSAGES)
    next(stream)
    with pytest.raises(httpx.ReadError, match="synthetic stream failure"):
        next(stream)
    result = span(exporter, ".stream_chat")
    assert result.status.status_code == StatusCode.ERROR
    assert len([e for e in result.events if e.name == "exception"]) == 1


@pytest.mark.asyncio
async def test_async_failure_is_preserved(traces, clients):
    exporter, _ = traces()
    clients[2]["mode"] = "error"
    stream = await model(clients).astream_chat(MESSAGES)
    await stream.__anext__()
    with pytest.raises(httpx.ReadError, match="synthetic stream failure"):
        await stream.__anext__()
    assert span(exporter, ".astream_chat").status.status_code == StatusCode.ERROR


@pytest.mark.asyncio
async def test_async_task_cancellation_is_immediate(traces, clients):
    exporter, instrumentor = traces()
    clients[2].update(mode="block", blocked=asyncio.Event())
    stream = await model(clients).astream_chat(MESSAGES)
    await stream.__anext__()
    task = asyncio.create_task(stream.__anext__())
    await asyncio.wait_for(clients[2]["blocked"].wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_cancelled(exporter, instrumentor, ".astream_chat")


@pytest.mark.parametrize("hidden", [False, True])
def test_real_openai_provider_and_detailed_usage(traces, clients, hidden):
    exporter, _ = traces(hidden)
    assert model(clients).chat(MESSAGES).message.content == ANSWER
    attrs = span(exporter, ".chat").attributes
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.usage.total_tokens"] == 18
    assert attrs["gen_ai.usage.cache_read_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens.reasoning"] == 3
    if hidden:
        assert PROMPT not in str(dict(attrs)) and ANSWER not in str(dict(attrs))


def test_real_azure_adapter(traces, clients):
    AzureOpenAI = pytest.importorskip("llama_index.llms.azure_openai").AzureOpenAI
    exporter, _ = traces()
    llm = model(clients, AzureOpenAI, engine="synthetic-deployment",
                azure_endpoint="https://synthetic.openai.azure.com", api_version="2024-02-01")
    llm.chat(MESSAGES)
    assert span(exporter, ".chat").attributes["gen_ai.provider.name"] == "azure.ai.openai"


def test_real_compatible_adapter_does_not_guess_from_model(traces, clients):
    OpenAILike = pytest.importorskip("llama_index.llms.openai_like").OpenAILike
    exporter, _ = traces()
    model(clients, OpenAILike, api_base="https://synthetic.invalid/v1",
          is_chat_model=True).chat(MESSAGES)
    results = exporter.get_finished_spans()
    assert results
    assert all(s.attributes["gen_ai.provider.name"] == "openai_compatible" for s in results)


@pytest.mark.parametrize("as_object", [False, True])
def test_usage_preserves_zero_counts_and_provider_cache_fields(as_object):
    from traceai_llamaindex._handler import _get_token_counts

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 4,
             "output_tokens_details": {"reasoning_tokens": 0}}
    if as_object:
        usage["output_tokens_details"] = SimpleNamespace(**usage["output_tokens_details"])
        usage = SimpleNamespace(**usage)
    assert dict(_get_token_counts(usage)) == {
        "gen_ai.usage.input_tokens": 4, "gen_ai.usage.output_tokens": 0,
        "gen_ai.usage.total_tokens": 0, "gen_ai.usage.cache_read_tokens": 0,
        "gen_ai.usage.cache_write_tokens": 4, "gen_ai.usage.output_tokens.reasoning": 0,
    }


def test_idle_cleanup_never_exports_success(traces, clients):
    exporter, instrumentor = traces()
    stream = model(clients).stream_chat(MESSAGES)
    next(stream)
    queued, = instrumentor._span_handler._export_queue.spans.values()
    queued._last_updated_at = time.time() - 61
    deadline = time.monotonic() + 2
    while not exporter.get_finished_spans() and time.monotonic() < deadline:
        time.sleep(0.01)
    result = span(exporter, ".stream_chat")
    assert result.status.status_code == StatusCode.ERROR
    assert result.attributes["llama_index.cancel_reason"] == "stream idle timeout"
    stream.close()


def test_queued_span_drop_is_not_lost(traces, clients):
    import inspect

    exporter, instrumentor = traces()
    stream = model(clients).stream_chat(MESSAGES)
    queued, = instrumentor._span_handler._export_queue.spans.values()
    instrumentor._span_handler.prepare_to_drop_span(
        queued.id_, inspect.signature(lambda: None).bind(), err=ValueError("synthetic drop")
    )
    result = span(exporter, ".stream_chat")
    assert result.status.status_code == StatusCode.ERROR
    assert "synthetic drop" in result.status.description
    stream.close()


def test_subclass_created_after_instrumentation_and_restore(traces, clients):
    from llama_index.core.llms.callbacks import llm_chat_callback

    original = OpenAI.__dict__["stream_chat"]
    exporter, instrumentor = traces()

    class LateOpenAI(OpenAI):
        @llm_chat_callback()
        def stream_chat(self, messages, **kwargs):
            return super().stream_chat(messages, **kwargs)

    llm = model(clients, LateOpenAI)
    stream = llm.stream_chat(MESSAGES)
    next(stream)
    stream.close()
    assert exporter.get_finished_spans()
    assert all(s.status.status_code == StatusCode.ERROR for s in exporter.get_finished_spans())
    assert not any(s.active for s in instrumentor._span_handler._export_queue.spans.values())
    instrumentor.uninstrument()
    assert OpenAI.__dict__["stream_chat"] is original
    exporter.clear()
    assert list(llm.stream_chat(MESSAGES))
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("endpoint,expected", [
    ("https://api.groq.com/openai/v1", "groq"),
    ("https://synthetic.openai.azure.com/openai", "azure.ai.openai"),
    ("https://api.groq.com.attacker.invalid/v1", "openai_compatible"),
])
def test_compatible_endpoint_config(traces, clients, endpoint, expected):
    OpenAILike = pytest.importorskip("llama_index.llms.openai_like").OpenAILike
    exporter, _ = traces()
    model(clients, OpenAILike, api_base=endpoint, is_chat_model=True).chat(MESSAGES)
    results = exporter.get_finished_spans()
    assert results
    assert all(s.attributes["gen_ai.provider.name"] == expected for s in results)


def test_failure_after_creating_a_nested_stream_finishes_children(traces, clients):
    from llama_index.core.llms.callbacks import llm_chat_callback

    exporter, instrumentor = traces()

    class FailingOpenAI(OpenAI):
        @llm_chat_callback()
        def stream_chat(self, messages, **kwargs):
            super().stream_chat(messages, **kwargs)
            raise ValueError("synthetic setup failure")

    with pytest.raises(ValueError, match="synthetic setup failure"):
        model(clients, FailingOpenAI).stream_chat(MESSAGES)
    assert exporter.get_finished_spans()
    assert all(s.status.status_code == StatusCode.ERROR for s in exporter.get_finished_spans())
    assert not any(s.active for s in instrumentor._span_handler._export_queue.spans.values())


def test_sync_generator_protocol_preserves_thrown_error(traces, clients):
    exporter, _ = traces()
    stream = model(clients).stream_chat(MESSAGES)
    assert stream.send(None).delta == "synthetic "
    error = ValueError("synthetic caller failure")
    with pytest.raises(ValueError) as caught:
        stream.throw(error)
    assert caught.value is error
    assert span(exporter, ".stream_chat").status.status_code == StatusCode.ERROR


@pytest.mark.asyncio
async def test_async_generator_protocol_preserves_thrown_error(traces, clients):
    exporter, _ = traces()
    stream = await model(clients).astream_chat(MESSAGES)
    assert (await stream.asend(None)).delta == "synthetic "
    error = ValueError("synthetic caller failure")
    with pytest.raises(ValueError) as caught:
        await stream.athrow(error)
    assert caught.value is error
    assert span(exporter, ".astream_chat").status.status_code == StatusCode.ERROR


def query_engine(clients):
    from llama_index.core import Document, VectorStoreIndex
    from llama_index.core.embeddings import MockEmbedding

    embed = MockEmbedding(embed_dim=3)
    index = VectorStoreIndex.from_documents([Document(text="synthetic source")], embed_model=embed)
    return index.as_query_engine(llm=model(clients), embed_model=embed, streaming=True)


def rag_spans(exporter, asynchronous=False):
    suffixes = (".aquery", "._aquery", ".asynthesize") if asynchronous else (
        ".query", "._query", ".synthesize"
    )
    return [s for s in exporter.get_finished_spans() if s.name.endswith(suffixes)]


def assert_rag_finished(exporter, instrumentor, status, asynchronous=False):
    results = rag_spans(exporter, asynchronous)
    assert len(results) == 3, [s.name for s in exporter.get_finished_spans()]
    assert all(s.status.status_code == status for s in results)
    assert not instrumentor._span_handler.open_spans
    pending = [s for s in instrumentor._span_handler._export_queue.spans.values() if s.active]
    assert not pending, [(s.id_, s.parent_id, s._parent.id_ if s._parent else None) for s in pending]
    model_spans = [s for s in exporter.get_finished_spans()
                   if s.name.endswith((".stream_chat", ".astream_chat", ".stream", ".astream"))]
    assert all(s.context.trace_id == results[0].context.trace_id for s in model_spans)
    return results


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("instrumented", [False, True])
def test_rag_stream_is_lazy_and_preserves_chunks(traces, clients, instrumented, hidden):
    exporter, instrumentor = traces(hidden)
    if not instrumented:
        instrumentor.uninstrument()
    response = query_engine(clients).query(PROMPT)
    assert clients[2]["requests"] == 0
    assert not rag_spans(exporter)
    chunks = list(response.response_gen)
    assert chunks == ["synthetic ", "answer", ""]
    assert clients[2]["requests"] == 1
    if instrumented:
        for result in assert_rag_finished(exporter, instrumentor, StatusCode.OK):
            assert result.end_time >= clients[2]["last_byte_at"]
            assert (ANSWER in result.attributes["output.value"]) is not hidden
        for result in exporter.get_finished_spans():
            if hidden:
                assert PROMPT not in str(dict(result.attributes))
                assert ANSWER not in str(dict(result.attributes))


@pytest.mark.asyncio
@pytest.mark.parametrize("instrumented", [False, True])
async def test_async_rag_stream_is_lazy_and_preserves_chunks(traces, clients, instrumented):
    exporter, instrumentor = traces()
    if not instrumented:
        instrumentor.uninstrument()
    response = await query_engine(clients).aquery(PROMPT)
    assert clients[2]["requests"] == 0
    assert clients[2]["last_byte_at"] == 0
    assert not rag_spans(exporter, True)
    chunks = [chunk async for chunk in response.response_gen]
    assert chunks == ["synthetic ", "answer", ""]
    if instrumented:
        for result in assert_rag_finished(exporter, instrumentor, StatusCode.OK, True):
            assert result.end_time >= clients[2]["last_byte_at"]
            assert ANSWER in result.attributes["output.value"]


@pytest.mark.parametrize("started", [False, True])
def test_rag_stream_close_finishes_query_and_children(traces, clients, started):
    exporter, instrumentor = traces()
    response = query_engine(clients).query(PROMPT)
    if started:
        assert next(response.response_gen) == "synthetic "
    assert not rag_spans(exporter)
    response.response_gen.close()
    for result in assert_rag_finished(exporter, instrumentor, StatusCode.ERROR):
        assert result.attributes["llama_index.cancelled"] is True
        assert all(e.name != "exception" for e in result.events)
    response.response_gen.close()
    assert len(rag_spans(exporter)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_async_rag_stream_close_finishes_query_and_children(traces, clients, started):
    exporter, instrumentor = traces()
    response = await query_engine(clients).aquery(PROMPT)
    if started:
        assert await response.response_gen.__anext__() == "synthetic "
    assert not rag_spans(exporter, True)
    await response.response_gen.aclose()
    for result in assert_rag_finished(exporter, instrumentor, StatusCode.ERROR, True):
        assert result.attributes["llama_index.cancelled"] is True
        assert all(e.name != "exception" for e in result.events)


def test_rag_stream_failure_reaches_query(traces, clients):
    exporter, instrumentor = traces()
    clients[2]["mode"] = "error"
    response = query_engine(clients).query(PROMPT)
    assert next(response.response_gen) == "synthetic "
    with pytest.raises(httpx.ReadError, match="synthetic stream failure"):
        next(response.response_gen)
    for result in assert_rag_finished(exporter, instrumentor, StatusCode.ERROR):
        assert len([e for e in result.events if e.name == "exception"]) == 1


@pytest.mark.asyncio
async def test_async_rag_stream_failure_reaches_query(traces, clients):
    exporter, instrumentor = traces()
    clients[2]["mode"] = "error"
    response = await query_engine(clients).aquery(PROMPT)
    assert await response.response_gen.__anext__() == "synthetic "
    with pytest.raises(httpx.ReadError, match="synthetic stream failure"):
        await response.response_gen.__anext__()
    assert_rag_finished(exporter, instrumentor, StatusCode.ERROR, True)


@pytest.mark.asyncio
async def test_async_rag_task_cancellation_reaches_query(traces, clients):
    exporter, instrumentor = traces()
    clients[2].update(mode="block", blocked=asyncio.Event())
    response = await query_engine(clients).aquery(PROMPT)
    assert await response.response_gen.__anext__() == "synthetic "
    task = asyncio.create_task(response.response_gen.__anext__())
    await asyncio.wait_for(clients[2]["blocked"].wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for result in assert_rag_finished(exporter, instrumentor, StatusCode.ERROR, True):
        assert result.attributes["llama_index.cancelled"] is True


def test_closing_one_rag_response_does_not_end_another(traces, clients):
    exporter, instrumentor = traces()
    engine = query_engine(clients)
    first = engine.query("first question")
    second = engine.query("second question")
    assert clients[2]["requests"] == 0
    first.response_gen.close()
    results = rag_spans(exporter)
    assert len(results) == 3
    assert all(s.status.status_code == StatusCode.ERROR for s in results)
    assert any(s.active for s in instrumentor._span_handler._export_queue.spans.values())
    assert "".join(second.response_gen) == ANSWER
    results = rag_spans(exporter)
    assert len(results) == 6
    assert sum(s.status.status_code == StatusCode.OK for s in results) == 3
    assert len({s.context.trace_id for s in results}) == 2
    assert not any(s.active for s in instrumentor._span_handler._export_queue.spans.values())
