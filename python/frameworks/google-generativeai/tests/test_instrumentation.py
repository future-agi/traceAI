import asyncio
import json

import google.generativeai as genai
import pytest
from google.generativeai import protos
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value
from opentelemetry.trace import StatusCode

from conftest import HAS_MODEL_VERSION, MODEL, call_response, one, text_response

IN = "gen_ai.input.messages"
OUT = "gen_ai.output.messages"


def test_generate_content_records_llm_span(spans, model, client):
    client.responses.append(text_response("hello back"))
    result = model.generate_content("hello", generation_config={"temperature": 0.25, "max_output_tokens": 50})
    assert result.text == "hello back"
    span, a = one(spans)
    assert a["gen_ai.span.kind"] == "LLM"
    assert a["gen_ai.provider.name"] == "google"
    assert a["gen_ai.request.model"] == MODEL
    if HAS_MODEL_VERSION:
        assert a["gen_ai.response.model"] == "gemini-1.5-flash-002"
    params = json.loads(a["gen_ai.request.parameters"])
    assert params["temperature"] == 0.25 and params["max_output_tokens"] == 50
    assert a["gen_ai.request.temperature"] == 0.25
    assert a["gen_ai.request.max_tokens"] == 50
    assert a[f"{IN}.0.message.role"] == "user"
    assert a[f"{IN}.0.message.content"] == "hello"
    assert a[f"{OUT}.0.message.role"] == "model"
    assert a[f"{OUT}.0.message.content"] == "hello back"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (5, 7, 12)
    assert list(a["gen_ai.response.finish_reasons"]) == ["STOP"]
    assert span.status.status_code != StatusCode.ERROR
    assert span.end_time is not None


def test_system_instruction_is_first_input_message(spans, client):
    m = genai.GenerativeModel(MODEL, system_instruction="be brief")
    m._client = client
    client.responses.append(text_response("ok"))
    m.generate_content("hi")
    _, a = one(spans)
    assert a[f"{IN}.0.message.role"] == "system"
    assert a[f"{IN}.0.message.content"] == "be brief"
    assert a[f"{IN}.1.message.content"] == "hi"


def test_tools_and_function_calls(spans, client):
    def get_weather(city: str):
        """Look up the weather."""
        return "sunny"

    m = genai.GenerativeModel(MODEL, tools=[get_weather])
    m._client = client
    client.responses.append(call_response("get_weather", {"city": "Paris"}))
    m.generate_content([
        {"role": "user", "parts": ["weather?"]},
        {"role": "model", "parts": [protos.Part(function_call=protos.FunctionCall(name="get_weather", args={"city": "Rome"}))]},
        {"role": "user", "parts": [protos.Part(function_response=protos.FunctionResponse(name="get_weather", response={"r": "rainy"}))]},
    ])
    _, a = one(spans)
    schema = json.loads(a["gen_ai.tool.definitions.0.tool.json_schema"])
    assert schema["name"] == "get_weather"
    assert a[f"{OUT}.0.message.tool_calls.0.tool_call.function.name"] == "get_weather"
    assert json.loads(a[f"{OUT}.0.message.tool_calls.0.tool_call.function.arguments"]) == {"city": "Paris"}
    assert json.loads(a[f"{IN}.1.message.tool_calls.0.tool_call.function.arguments"]) == {"city": "Rome"}
    assert "rainy" in a[f"{IN}.2.message.content"]


def test_binary_parts_are_noted_without_bytes(spans, model, client):
    client.responses.append(text_response("seen"))
    model.generate_content([{"role": "user", "parts": [{"inline_data": {"mime_type": "image/png", "data": b"RAW-IMAGE-BYTES"}}, "what is this?"]}])
    span, a = one(spans)
    assert "RAW-IMAGE-BYTES" not in json.dumps(a, default=str)
    assert "what is this?" in json.dumps(a, default=str)


def test_provider_error_is_reraised_with_error_status(spans, model, client):
    client.error = RuntimeError("upstream exploded")
    with pytest.raises(RuntimeError, match="upstream exploded"):
        model.generate_content("hello")
    span, a = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert a["error.type"] == "RuntimeError" or "upstream exploded" in (span.status.description or "")
    assert span.end_time is not None


def test_stream_aggregates_output_and_records_time_to_first_token(spans, model, client):
    client.chunks = [text_response("Hel", usage=None), text_response("lo ", usage=None), text_response("world", usage=(4, 9))]
    pieces = [chunk.text for chunk in model.generate_content("hi", stream=True)]
    assert pieces == ["Hel", "lo ", "world"]
    span, a = one(spans)
    assert a[f"{OUT}.0.message.content"] == "Hello world"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == (4, 9)
    assert a["gen_ai.server.time_to_first_token"] >= 0
    assert list(a["gen_ai.response.finish_reasons"]) == ["STOP"]


def test_stream_span_ends_when_response_is_resolved(spans, model, client):
    client.chunks = [text_response("a", usage=None), text_response("b", usage=(1, 2))]
    response = model.generate_content("hi", stream=True)
    response.resolve()
    _, a = one(spans)
    assert a[f"{OUT}.0.message.content"] == "ab"


def test_stream_open_error(spans, model, client):
    client.error = RuntimeError("no stream")
    with pytest.raises(RuntimeError):
        model.generate_content("hi", stream=True)
    span, _ = one(spans)
    assert span.status.status_code == StatusCode.ERROR


def test_stream_error_midway_marks_error_and_reraises(spans, model, client):
    def chunks():
        yield text_response("part", usage=None)
        raise ConnectionError("dropped")

    client.chunks = chunks()
    with pytest.raises(ConnectionError):
        for _ in model.generate_content("hi", stream=True):
            pass
    span, a = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert a[f"{OUT}.0.message.content"] == "part"


def test_async_generate_and_stream(spans, amodel, aclient):
    aclient.responses.append(text_response("async hello"))
    aclient.chunks = [text_response("x", usage=None), text_response("y", usage=(2, 3))]

    async def run():
        r = await amodel.generate_content_async("hi")
        s = await amodel.generate_content_async("hi", stream=True)
        return r.text, [c.text async for c in s]

    text, pieces = asyncio.run(run())
    assert text == "async hello" and pieces == ["x", "y"]
    finished = spans.get_finished_spans()
    assert len(finished) == 2
    first, second = (dict(s.attributes) for s in finished)
    assert first[f"{OUT}.0.message.content"] == "async hello"
    assert second[f"{OUT}.0.message.content"] == "xy"
    assert second["gen_ai.server.time_to_first_token"] >= 0


def test_chat_session_traces_each_turn_once(spans, model, client):
    client.responses.extend([text_response("one"), text_response("two")])
    chat = model.start_chat()
    chat.send_message("first")
    chat.send_message("second")
    finished = spans.get_finished_spans()
    assert len(finished) == 2
    second = dict(finished[1].attributes)
    assert second[f"{IN}.0.message.content"] == "first"
    assert second[f"{IN}.1.message.content"] == "one"
    assert second[f"{IN}.2.message.content"] == "second"


def test_chat_session_stream_and_async(spans, model, client, amodel, aclient):
    client.chunks = [text_response("s1", usage=None), text_response("s2", usage=(1, 1))]
    for _ in model.start_chat().send_message("hi", stream=True):
        pass
    aclient.responses.append(text_response("async chat"))
    asyncio.run(amodel.start_chat().send_message_async("hi"))
    finished = spans.get_finished_spans()
    assert [dict(s.attributes)[f"{OUT}.0.message.content"] for s in finished] == ["s1s2", "async chat"]


def test_count_tokens_is_not_traced(spans, model, client):
    client.count_tokens = lambda request, **kw: protos.CountTokensResponse(total_tokens=3)
    model.count_tokens("hello")
    assert spans.get_finished_spans() == ()


def test_embed_content_records_embedding_span(spans, client):
    genai.embed_content(model="models/text-embedding-004", content="embed me", client=client)
    span, a = one(spans)
    assert a["gen_ai.span.kind"] == "EMBEDDING"
    assert a["gen_ai.request.model"] == "models/text-embedding-004"
    assert a["embedding.embeddings.0.embedding.text"] == "embed me"
    assert a["gen_ai.embeddings.dimension.count"] == 3
    assert "0.125" not in json.dumps(a, default=str)


def test_embed_content_batch_and_async(spans, client, aclient):
    genai.embed_content(model="models/text-embedding-004", content=["a", "b"], client=client)
    asyncio.run(genai.embed_content_async(model="models/text-embedding-004", content="c", client=aclient))
    finished = spans.get_finished_spans()
    assert len(finished) == 2
    batch = dict(finished[0].attributes)
    assert batch["embedding.embeddings.0.embedding.text"] == "a"
    assert batch["embedding.embeddings.1.embedding.text"] == "b"


def test_suppressed_instrumentation_creates_no_span(spans, model, client):
    client.responses.append(text_response("quiet"))
    token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert model.generate_content("hi").text == "quiet"
    finally:
        detach(token)
    assert spans.get_finished_spans() == ()


def test_uninstrument_restores_originals(make_spans, client):
    from google.generativeai import embedding
    from google.generativeai.generative_models import GenerativeModel

    originals = (GenerativeModel.generate_content, GenerativeModel.generate_content_async, embedding.embed_content, genai.embed_content)
    exporter = make_spans()
    assert GenerativeModel.generate_content is not originals[0]
    from traceai_google_generativeai import GoogleGenerativeAIInstrumentor

    GoogleGenerativeAIInstrumentor().uninstrument()
    assert (GenerativeModel.generate_content, GenerativeModel.generate_content_async, embedding.embed_content, genai.embed_content) == originals
    assert exporter.get_finished_spans() == ()
