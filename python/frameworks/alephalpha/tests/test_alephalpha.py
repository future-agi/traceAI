"""Aleph Alpha SDK instrumentation over the real SDK clients with a loopback HTTP stub."""
import asyncio
import gc
import json

import aleph_alpha_client as aa
from aleph_alpha_client import chat as chat_module
from aleph_alpha_client.chat import ChatRequest, Message, Role
from aleph_alpha_client.embedding import InstructableEmbeddingRequest
import pytest
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import StatusCode

from conftest import (
    TOKEN, Reply, attrs, blob, chat_body, chat_chunk, completion_body, sse,
)
from fi_instrumentation import TraceConfig

OUT = "gen_ai.output.messages.0.message"
MESSAGES = [Message(role=Role.User, content="Capital of Japan?")]
HAS_STREAMING = hasattr(aa.AsyncClient, "complete_with_streaming")
HAS_TOOLS = hasattr(chat_module, "ToolCall")
HAS_IMAGES = hasattr(chat_module, "_message_content_to_json")
HAS_CHAT_FINISH = hasattr(chat_module, "FinishReason")
needs_tools = pytest.mark.skipif(not HAS_TOOLS, reason="chat tool calls are not in this SDK")


def one(spans):
    finished = spans.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0]


def run(coro_fn, aclient):
    async def go():
        async with aclient() as c:
            return await coro_fn(c)

    return asyncio.run(go())


def complete_request(text="Capital of Japan?", **kwargs):
    return aa.CompletionRequest(prompt=aa.Prompt.from_text(text), **kwargs)


# --- complete ----------------------------------------------------------------------------------

def test_complete_records_model_parameters_usage_and_content(spans, stub, client):
    stub.reply(completion_body("Tokyo"))
    response = client.complete(
        complete_request(maximum_tokens=16, temperature=0.2, top_p=0.9, top_k=5, stop_sequences=["END"]),
        model="luminous-base",
    )
    assert response.completions[0].completion == "Tokyo"
    path, sent, headers = stub.requests[0]
    assert path == "/complete" and sent["model"] == "luminous-base" and sent["maximum_tokens"] == 16
    span = one(spans)
    a = attrs(span)
    assert span.name == "alephalpha.text_completion"
    assert a["gen_ai.span.kind"] == "LLM"
    assert a["gen_ai.provider.name"] == "aleph_alpha"
    assert a["gen_ai.operation.name"] == "text_completion"
    assert a["gen_ai.request.model"] == "luminous-base"
    assert a["gen_ai.request.max_tokens"] == 16
    assert a["gen_ai.request.temperature"] == 0.2
    assert a["gen_ai.request.top_p"] == 0.9
    assert a["gen_ai.request.top_k"] == 5
    assert list(a["gen_ai.request.stop_sequences"]) == ["END"]
    assert a["gen_ai.response.model"] == "luminous-served"
    assert list(a["gen_ai.response.finish_reasons"]) == ["maximum_tokens"]
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (4, 2, 6)
    assert a["input.value"] == "Capital of Japan?"
    assert a["output.value"] == "Tokyo"
    assert TOKEN not in blob(span)
    assert span.status.status_code != StatusCode.ERROR


def test_complete_async(spans, stub, aclient):
    stub.reply(completion_body("async Tokyo"))
    result = run(lambda c: c.complete(complete_request(), model="m"), aclient)
    assert result.completions[0].completion == "async Tokyo"
    a = attrs(one(spans))
    assert a["output.value"] == "async Tokyo" and a["gen_ai.usage.total_tokens"] == 6


def test_complete_with_keyword_arguments(spans, stub, client):
    stub.reply(completion_body())
    client.complete(request=complete_request(), model="kw-model")
    assert attrs(one(spans))["gen_ai.request.model"] == "kw-model"


def test_non_text_prompt_items_are_not_serialized(spans, stub, client):
    stub.reply(completion_body())
    prompt = aa.Prompt([aa.Text.from_text("describe"), aa.Tokens.from_token_ids([1, 2, 3])])
    client.complete(aa.CompletionRequest(prompt=prompt), model="m")
    assert "input.value" not in attrs(one(spans))


# --- chat --------------------------------------------------------------------------------------

def test_chat_records_request_response_and_usage(spans, stub, client):
    stub.reply(chat_body("Tokyo"))
    request = ChatRequest(model="pharia", messages=MESSAGES, maximum_tokens=8, temperature=0.1, top_p=0.5)
    response = client.chat(request, model="pharia-1-llm-7b-control")
    assert response.message.content == "Tokyo"
    span = one(spans)
    a = attrs(span)
    assert span.name == "alephalpha.chat"
    assert a["gen_ai.span.kind"] == "LLM" and a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.model"] == "pharia-1-llm-7b-control"
    assert a["gen_ai.request.max_tokens"] == 8 and a["gen_ai.request.temperature"] == 0.1
    assert json.loads(a["input.value"]) == [{"role": "user", "content": "Capital of Japan?"}]
    assert a["output.value"] == "Tokyo"
    assert list(a["gen_ai.response.finish_reasons"]) == ["stop"]
    assert TOKEN not in blob(span)


def test_chat_async(spans, stub, aclient):
    stub.reply(chat_body("Tokyo"))
    result = run(lambda c: c.chat(ChatRequest(model="m", messages=MESSAGES), model="m"), aclient)
    assert result.message.content == "Tokyo"
    assert attrs(one(spans))["output.value"] == "Tokyo"


@needs_tools
def test_chat_tool_calls_are_recorded(spans, stub, client):
    stub.reply(chat_body(
        "", finish="tool_calls",
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'}}],
    ))
    client.chat(ChatRequest(model="m", messages=MESSAGES), model="m")
    a = attrs(one(spans))
    prefix = f"{OUT}.tool_calls.0.tool_call"
    assert a[f"{prefix}.id"] == "call-1"
    assert a[f"{prefix}.function.name"] == "get_weather"
    assert json.loads(a[f"{prefix}.function.arguments"]) == {"city": "Tokyo"}


@needs_tools
def test_tool_arguments_are_hidden_with_outputs(instrument, stub, client):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    stub.reply(chat_body("", tool_calls=[{"id": "c", "type": "function", "function": {"name": "lookup", "arguments": '{"ssn": "SECRET-ARG"}'}}]))
    client.chat(ChatRequest(model="m", messages=MESSAGES), model="m")
    assert "SECRET-ARG" not in blob(one(spans))


@pytest.mark.skipif(not HAS_IMAGES, reason="chat images are not in this SDK")
def test_multimodal_messages_are_not_serialized(spans, stub, client):
    from PIL import Image

    stub.reply(chat_body())
    image = Image.new("RGB", (2, 2), (255, 0, 0))
    message = Message(role=Role.User, content=["look", image])
    client.chat(ChatRequest(model="m", messages=[message]), model="m")
    span = one(spans)
    assert "input.value" not in attrs(span)
    assert "data:image" not in blob(span)


# --- embeddings and evaluate -------------------------------------------------------------------

def embed_body():
    return {"model_version": "emb-served", "embedding": [0.1, 0.2, 0.3], "num_tokens_prompt_total": 7}


def test_semantic_embed_is_an_embedding_span_without_vectors(spans, stub, client):
    stub.reply(embed_body())
    request = aa.SemanticEmbeddingRequest(prompt=aa.Prompt.from_text("hello world"), representation=aa.SemanticRepresentation.Document)
    client.semantic_embed(request, model="luminous-base")
    span = one(spans)
    a = attrs(span)
    assert span.name == "alephalpha.embeddings"
    assert a["gen_ai.span.kind"] == "EMBEDDING"
    assert a["gen_ai.operation.name"] == "embeddings"
    assert a["gen_ai.provider.name"] == "aleph_alpha"
    assert a["gen_ai.request.model"] == "luminous-base"
    assert a["gen_ai.response.model"] == "emb-served"
    assert a["gen_ai.usage.input_tokens"] == 7
    assert a["input.value"] == "hello world"
    assert "0.2" not in blob(span)


def test_embed_async_and_batch(spans, stub, aclient):
    stub.reply({"model_version": "emb-served", "embeddings": [[0.1], [0.2]], "num_tokens_prompt_total": 9})
    request = aa.BatchSemanticEmbeddingRequest(
        prompts=[aa.Prompt.from_text("a"), aa.Prompt.from_text("b")], representation=aa.SemanticRepresentation.Query
    )
    result = run(lambda c: c.batch_semantic_embed(request, model="m"), aclient)
    assert len(result.embeddings) == 2
    finished = spans.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]  # the batch helper's inner requests are not traced
    a = attrs(finished[0])
    assert a["gen_ai.usage.input_tokens"] == 9
    assert json.loads(a["input.value"]) == ["a", "b"]


def test_embed_with_layers_and_instructable_embed(spans, stub, client):
    stub.reply({"model_version": "v", "embeddings": {"layer_-1": {"mean": [0.5]}}, "num_tokens_prompt_total": 3, "tokens": None})
    client.embed(aa.EmbeddingRequest(prompt=aa.Prompt.from_text("x"), layers=[-1], pooling=["mean"]), model="m")
    assert attrs(one(spans))["gen_ai.span.kind"] == "EMBEDDING"
    spans.clear()
    if not hasattr(aa.Client, "instructable_embed"):
        pytest.skip("instructable_embed is not in this SDK")
    stub.reply(embed_body())
    client.instructable_embed(InstructableEmbeddingRequest(input=aa.Prompt.from_text("doc"), instruction="Summarise"), model="m")
    assert attrs(one(spans))["gen_ai.usage.input_tokens"] == 7


def test_embeddings_v2_reports_usage(spans, stub, client):
    if not hasattr(aa.Client, "embeddings"):
        pytest.skip("embeddings (v2) is not in this SDK")
    stub.reply({"object": "list", "model": "v2-served", "data": [{"object": "embedding", "embedding": [0.1], "index": 0}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4}})
    client.embeddings(aa.EmbeddingV2Request(input="hello"), model="m")
    a = attrs(one(spans))
    assert a["gen_ai.response.model"] == "v2-served"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.total_tokens"]) == (4, 4)
    assert a["input.value"] == "hello"


def test_evaluate_sync_and_async(spans, stub, client, aclient):
    stub.reply({"model_version": "eval-served", "result": {"log_probability": -1.5, "log_perplexity": 0.5}, "num_tokens_prompt_total": 5})
    request = aa.EvaluationRequest(prompt=aa.Prompt.from_text("hello"), completion_expected=" world")
    client.evaluate(request, model="luminous-base")
    span = one(spans)
    a = attrs(span)
    assert span.name == "alephalpha.evaluate"
    assert a["gen_ai.span.kind"] == "LLM" and a["gen_ai.operation.name"] == "evaluate"
    assert a["gen_ai.usage.input_tokens"] == 5
    assert json.loads(a["input.value"]) == {"prompt": "hello", "completion_expected": " world"}
    assert json.loads(a["output.value"])["log_probability"] == -1.5
    spans.clear()
    run(lambda c: c.evaluate(request, model="m"), aclient)
    assert attrs(one(spans))["gen_ai.response.model"] == "eval-served"


# --- streaming (async client only) -------------------------------------------------------------

pytestmark_stream = pytest.mark.skipif(not HAS_STREAMING, reason="streaming is not in this SDK")


def completion_stream():
    return sse(
        {"type": "stream_chunk", "index": 0, "completion": "Tok"},
        {"type": "stream_chunk", "index": 0, "completion": "yo"},
        {"type": "stream_summary", "index": 0, "model_version": "luminous-served", "finish_reason": "maximum_tokens"},
        {"type": "completion_summary", "num_tokens_prompt_total": 6, "num_tokens_generated": 2},
    )


def chat_stream():
    return sse(
        chat_chunk("", role="assistant"),
        chat_chunk("Tok"),
        chat_chunk("yo"),
        chat_chunk(finish="stop"),
        chat_chunk(usage={"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}),
    )


@pytestmark_stream
def test_complete_stream_ends_on_exhaustion_with_assembled_output(spans, stub, aclient):
    stub.reply(completion_stream())

    async def go(c):
        stream = c.complete_with_streaming(complete_request(), model="luminous-base")
        first = await stream.__anext__()
        assert first.completion == "Tok"
        assert spans.get_finished_spans() == ()
        return [first] + [item async for item in stream]

    items = run(go, aclient)
    assert len(items) == 4
    span = one(spans)
    a = attrs(span)
    assert span.name == "alephalpha.text_completion"
    assert a["output.value"] == "Tokyo"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (6, 2, 8)
    assert list(a["gen_ai.response.finish_reasons"]) == ["maximum_tokens"]
    assert a["gen_ai.response.model"] == "luminous-served"
    assert "alephalpha.stream.incomplete" not in a


@pytestmark_stream
def test_chat_stream_assembles_output_usage_and_finish(spans, stub, aclient):
    stub.reply(chat_stream())

    async def go(c):
        return [item async for item in c.chat_with_streaming(ChatRequest(model="m", messages=MESSAGES), model="m")]

    run(go, aclient)
    span = one(spans)
    a = attrs(span)
    assert span.name == "alephalpha.chat"
    assert a["output.value"] == "Tokyo"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (6, 2, 8)
    if HAS_CHAT_FINISH:
        assert list(a["gen_ai.response.finish_reasons"]) == ["stop"]


@pytestmark_stream
@needs_tools
def test_streamed_tool_calls_are_recorded(spans, stub, aclient):
    stub.reply(sse(
        chat_chunk("", role="assistant"),
        chat_chunk(tool_calls=[{"index": 0, "id": "call-9", "type": "function", "function": {"name": "get_weather", "arguments": '{"ci'}}]),
        chat_chunk(tool_calls=[{"index": 0, "function": {"arguments": 'ty": "Oslo"}'}}]),
        chat_chunk(finish="tool_calls"),
    ))

    async def go(c):
        return [i async for i in c.chat_with_streaming(ChatRequest(model="m", messages=MESSAGES), model="m")]

    run(go, aclient)
    a = attrs(one(spans))
    prefix = f"{OUT}.tool_calls.0.tool_call"
    assert a[f"{prefix}.id"] == "call-9" and a[f"{prefix}.function.name"] == "get_weather"
    assert json.loads(a[f"{prefix}.function.arguments"]) == {"city": "Oslo"}


@pytestmark_stream
def test_stream_early_exit_aclose_ends_span_incomplete_once(spans, stub, aclient):
    stub.reply(completion_stream())

    async def go(c):
        stream = c.complete_with_streaming(complete_request(), model="m")
        await stream.__anext__()
        assert spans.get_finished_spans() == ()
        await stream.aclose()
        await stream.aclose()

    run(go, aclient)
    span = one(spans)
    assert attrs(span)["alephalpha.stream.incomplete"] is True
    assert attrs(span)["output.value"] == "Tok"


@pytestmark_stream
def test_stream_break_out_of_async_for_ends_span_when_collected(spans, stub, aclient):
    stub.reply(completion_stream())

    async def go(c):
        stream = c.complete_with_streaming(complete_request(), model="m")
        async for _item in stream:
            break
        del stream
        gc.collect()
        await asyncio.sleep(0)

    run(go, aclient)
    assert attrs(one(spans))["alephalpha.stream.incomplete"] is True


@pytestmark_stream
def test_stream_never_iterated_is_ended_when_collected(spans, stub, aclient):
    stub.reply(completion_stream())

    async def go(c):
        stream = c.chat_with_streaming(ChatRequest(model="m", messages=MESSAGES), model="m")
        assert spans.get_finished_spans() == ()
        del stream
        gc.collect()

    run(go, aclient)
    assert attrs(one(spans))["alephalpha.stream.incomplete"] is True


@pytestmark_stream
def test_stream_midway_error_is_reraised_and_recorded(spans, stub, aclient):
    stub.reply(Reply(raw=b'data: {"type": "stream_chunk", "index": 0, "completion": "a"}\n\nGARBAGE SECRETDETAIL\n\n',
                     content_type="text/event-stream"))

    async def go(c):
        async for _ in c.complete_with_streaming(complete_request(), model="m"):
            pass

    with pytest.raises(ValueError):
        run(go, aclient)
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "ValueError"
    assert "SECRETDETAIL" not in blob(span)


@pytestmark_stream
def test_stream_hidden_outputs(instrument, stub, aclient):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    stub.reply(completion_stream())

    async def go(c):
        return [i async for i in c.complete_with_streaming(complete_request(), model="m")]

    run(go, aclient)
    span = one(spans)
    assert "Tokyo" not in blob(span)
    assert attrs(span)["gen_ai.usage.total_tokens"] == 8


@pytestmark_stream
def test_stream_http_error_is_recorded(spans, stub, aclient):
    stub.reply(Reply(500, {"error": "SECRETDETAIL"}))

    async def go(c):
        async for _ in c.complete_with_streaming(complete_request(), model="m"):
            pass

    with pytest.raises(Exception):
        run(go, aclient)
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert "SECRETDETAIL" not in blob(span)


# --- errors ------------------------------------------------------------------------------------

@pytest.mark.parametrize("status, error_class", [(402, "QuotaError"), (503, "BusyError"), (500, "RuntimeError")])
def test_status_errors_are_reraised_unchanged(spans, stub, client, status, error_class):
    stub.reply(Reply(status, {"error": "failure with SECRETDETAIL"}))
    with pytest.raises(Exception) as raised:
        client.complete(complete_request(), model="m")
    assert type(raised.value).__name__ == error_class
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == error_class
    assert "SECRETDETAIL" not in blob(span)
    assert span.end_time is not None


def test_async_error_and_connection_error(spans, stub, aclient):
    stub.reply(Reply(500, {"error": "x"}))
    with pytest.raises(Exception) as raised:
        run(lambda c: c.chat(ChatRequest(model="m", messages=MESSAGES), model="m"), aclient)
    assert attrs(one(spans))["error.type"] == type(raised.value).__name__
    spans.clear()
    dead = aa.Client(token=TOKEN, host="http://127.0.0.1:9", total_retries=0)
    with pytest.raises(Exception) as raised:
        dead.complete(complete_request(), model="m")
    assert attrs(one(spans))["error.type"] == type(raised.value).__name__


def test_invalid_arguments_still_raise_sdk_errors(spans, client):
    with pytest.raises(TypeError):
        client.complete(complete_request())  # model is required
    assert one(spans).status.status_code == StatusCode.ERROR


# --- privacy -----------------------------------------------------------------------------------

@pytest.mark.parametrize("config, hidden, visible", [
    (TraceConfig(hide_inputs=True), "TOPSECRET-IN", "TOPSECRET-OUT"),
    (TraceConfig(hide_outputs=True), "TOPSECRET-OUT", "TOPSECRET-IN"),
    (TraceConfig(hide_input_messages=True), "TOPSECRET-IN", "TOPSECRET-OUT"),
    (TraceConfig(hide_output_text=True), "TOPSECRET-OUT", "TOPSECRET-IN"),
])
def test_hidden_content_is_not_exported(instrument, stub, client, config, hidden, visible):
    spans = instrument(config=config)
    stub.reply(chat_body("TOPSECRET-OUT"))
    client.chat(ChatRequest(model="m", messages=[Message(role=Role.User, content="TOPSECRET-IN")]), model="m")
    span = one(spans)
    assert hidden not in blob(span)
    assert visible in blob(span)
    assert attrs(span)["gen_ai.request.model"] == "m"
    assert list(attrs(span)["gen_ai.response.finish_reasons"]) == ["stop"]


def test_hidden_inputs_for_embeddings_and_evaluate(instrument, stub, client):
    spans = instrument(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    stub.reply({"model_version": "v", "result": {"log_probability": -9.25}, "num_tokens_prompt_total": 1})
    client.evaluate(aa.EvaluationRequest(prompt=aa.Prompt.from_text("TOPSECRET-IN"), completion_expected="TOPSECRET-EXP"), model="m")
    stub.reply(embed_body())
    client.semantic_embed(aa.SemanticEmbeddingRequest(prompt=aa.Prompt.from_text("TOPSECRET-IN"), representation=aa.SemanticRepresentation.Query), model="m")
    for span in spans.get_finished_spans():
        assert "TOPSECRET" not in blob(span) and "9.25" not in blob(span)


# --- lifecycle ---------------------------------------------------------------------------------

def test_only_the_outermost_call_is_traced(monkeypatch, instrument, stub, client):
    # An SDK method that calls another traced method (a wrapper written before instrumenting)
    # yields one span, not two.
    monkeypatch.setattr(aa.Client, "chat", lambda self, request, model: self.complete(request, model))
    spans = instrument()
    stub.reply(completion_body())
    client.chat(complete_request(), "m")
    assert len(spans.get_finished_spans()) == 1


def test_suppressed_instrumentation_creates_no_span(spans, stub, client):
    stub.reply(completion_body())
    token = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        client.complete(complete_request(), model="m")
    finally:
        context_api.detach(token)
    assert spans.get_finished_spans() == ()


def test_tracing_failure_never_breaks_the_call(spans, stub, client, monkeypatch):
    stub.reply(completion_body("fine"))
    import traceai_alephalpha

    def boom(*args, **kwargs):
        raise RuntimeError("tracing bug")

    monkeypatch.setattr(traceai_alephalpha._Recorder, "observe", boom)
    monkeypatch.setattr(traceai_alephalpha, "_request", boom)
    assert client.complete(complete_request(), model="m").completions[0].completion == "fine"
    assert len(spans.get_finished_spans()) == 1


def test_uninstrument_restores_the_sdk_and_can_reinstrument(provider, stub, client):
    from traceai_alephalpha import AlephAlphaInstrumentor

    stub.reply(completion_body())
    original = aa.Client.__dict__["complete"]
    instrumentor = AlephAlphaInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    assert aa.Client.__dict__["complete"] is not original
    instrumentor.uninstrument()
    assert aa.Client.__dict__["complete"] is original
    client.complete(complete_request(), model="m")
    assert provider.exporter.get_finished_spans() == ()
    instrumentor.instrument(tracer_provider=provider)
    client.complete(complete_request(), model="m")
    instrumentor.uninstrument()
    assert len(provider.exporter.get_finished_spans()) == 1


def test_dependency_declaration():
    from traceai_alephalpha import AlephAlphaInstrumentor, __version__

    assert any(dep.startswith("aleph-alpha-client") for dep in AlephAlphaInstrumentor().instrumentation_dependencies())
    assert __version__


# --- partial output on failure, and every choice ------------------------------------------------
def multi_completion_body(texts=("Tokyo", "Kyoto"), finishes=("maximum_tokens", "end_of_text")):
    body = completion_body()
    body["completions"] = [
        {"completion": t, "finish_reason": f} for t, f in zip(texts, finishes)
    ]
    return body


def test_every_choice_text_and_finish_reason_is_recorded(spans, stub, client):
    stub.reply(multi_completion_body())
    client.complete(complete_request(n=2), model="m")
    a = attrs(one(spans))
    assert a["output.value"] == "Tokyo"
    assert a["gen_ai.output.messages.0.message.content"] == "Tokyo"
    assert a["gen_ai.output.messages.1.message.content"] == "Kyoto"
    assert list(a["gen_ai.response.finish_reasons"]) == ["maximum_tokens", "end_of_text"]


def test_single_choice_has_no_per_choice_attributes(spans, stub, client):
    stub.reply(completion_body("Tokyo"))
    client.complete(complete_request(), model="m")
    assert "gen_ai.output.messages.0.message.content" not in attrs(one(spans))


def test_every_choice_text_is_hidden_with_outputs(instrument, stub, client):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    stub.reply(multi_completion_body())
    client.complete(complete_request(n=2), model="m")
    text = blob(one(spans))
    assert "Tokyo" not in text and "Kyoto" not in text


@pytestmark_stream
def test_streamed_choices_are_recorded_per_index(spans, stub, aclient):
    stub.reply(sse(
        {"type": "stream_chunk", "index": 0, "completion": "To"},
        {"type": "stream_chunk", "index": 1, "completion": "Ky"},
        {"type": "stream_chunk", "index": 0, "completion": "kyo"},
        {"type": "stream_chunk", "index": 1, "completion": "oto"},
        {"type": "stream_summary", "index": 0, "model_version": "m", "finish_reason": "maximum_tokens"},
        {"type": "stream_summary", "index": 1, "model_version": "m", "finish_reason": "end_of_text"},
        {"type": "completion_summary", "num_tokens_prompt_total": 6, "num_tokens_generated": 4},
    ))

    async def go(c):
        return [i async for i in c.complete_with_streaming(complete_request(n=2), model="m")]

    run(go, aclient)
    a = attrs(one(spans))
    assert a["output.value"] == "Tokyo"
    assert a["gen_ai.output.messages.0.message.content"] == "Tokyo"
    assert a["gen_ai.output.messages.1.message.content"] == "Kyoto"


@pytestmark_stream
def test_stream_midway_error_keeps_the_partial_output(spans, stub, aclient):
    stub.reply(Reply(raw=b'data: {"type": "stream_chunk", "index": 0, "completion": "Tok"}\n\n'
                         b'data: {"type": "stream_chunk", "index": 0, "completion": "yo"}\n\n'
                         b'GARBAGE SECRETDETAIL\n\n', content_type="text/event-stream"))

    async def go(c):
        async for _ in c.complete_with_streaming(complete_request(), model="m"):
            pass

    with pytest.raises(ValueError):
        run(go, aclient)
    span = one(spans)
    a = attrs(span)
    assert span.status.status_code == StatusCode.ERROR
    assert a["error.type"] == "ValueError"
    assert a["output.value"] == "Tokyo"
    assert "SECRETDETAIL" not in blob(span)


@pytestmark_stream
def test_stream_midway_error_partial_output_is_hidden_with_outputs(instrument, stub, aclient):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    stub.reply(Reply(raw=b'data: {"type": "stream_chunk", "index": 0, "completion": "Tok"}\n\nGARBAGE\n\n',
                     content_type="text/event-stream"))

    async def go(c):
        async for _ in c.complete_with_streaming(complete_request(), model="m"):
            pass

    with pytest.raises(ValueError):
        run(go, aclient)
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert "Tok" not in blob(span)
