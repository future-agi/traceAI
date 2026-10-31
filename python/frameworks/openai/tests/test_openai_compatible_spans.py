"""Span contract for traceai-openai against OpenAI-compatible providers.

Every test drives the real ``openai`` SDK through ``httpx.MockTransport`` and
exports through a real OpenTelemetry SDK pipeline, so the attributes checked
here are exactly what the instrumentor hands to the exporter.
"""

import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_openai import OpenAIInstrumentor

REQUEST_MODEL = "vendor/requested-model"
RESPONSE_MODEL = "vendor/served-model-2026-10-01"
ANSWER = "Tokyo"
VECTOR = [0.25, -0.5, 0.125]


@pytest.fixture
def make_tracer():
    instrumentors = []

    def make(config=None):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
        instrumentors.append(instrumentor)
        return exporter

    yield make
    for instrumentor in instrumentors:
        instrumentor.uninstrument()


def client(handler, base_url="https://inference.baseten.co/v1"):
    return openai.OpenAI(
        base_url=base_url,
        api_key="test-vendor-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )


def async_client(handler, base_url="https://inference.baseten.co/v1"):
    return openai.AsyncOpenAI(
        base_url=base_url,
        api_key="test-vendor-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )


def only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [span.name for span in spans]
    return spans[0], dict(spans[0].attributes)


def chat_completion(model=RESPONSE_MODEL):
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": ANSWER}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def chat_stream(model=RESPONSE_MODEL):
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                      "finish_reason": None}]}
        for text in ("Tok", "yo")
    ]
    chunks.append({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def responses_body(text=ANSWER, model=RESPONSE_MODEL):
    return {
        "id": "resp_1", "object": "response", "created_at": 1, "model": model,
        "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "output": [{
            "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


def responses_stream():
    final = responses_body()
    events = [
        {"type": "response.created", "sequence_number": 0,
         "response": {**final, "status": "in_progress", "output": []}},
        {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg_1",
         "output_index": 0, "content_index": 0, "delta": "Tok", "logprobs": []},
        {"type": "response.output_text.delta", "sequence_number": 2, "item_id": "msg_1",
         "output_index": 0, "content_index": 0, "delta": "yo", "logprobs": []},
        {"type": "response.completed", "sequence_number": 3, "response": final},
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def embeddings_body(*, with_index=True, with_usage=True, model=RESPONSE_MODEL):
    item = {"object": "embedding", "embedding": VECTOR}
    if with_index:
        item["index"] = 0
    body = {"object": "list", "data": [item], "model": model}
    if with_usage:
        body["usage"] = {"prompt_tokens": 3, "total_tokens": 3}
    return body


def unauthorized(_request):
    return httpx.Response(401, json={"error": {"message": "Invalid API key",
                                               "type": "authentication_error"}})


# --- gen_ai.request.model on streamed and failed Chat Completions -------------------------


def test_non_streamed_chat_keeps_the_served_model(make_tracer):
    exporter = make_tracer()
    with client(lambda _: httpx.Response(200, json=chat_completion())) as c:
        c.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}])
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL


def test_streamed_chat_records_the_requested_model(make_tracer):
    exporter = make_tracer()
    with client(lambda _: chat_stream()) as c:
        stream = c.chat.completions.create(
            model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}], stream=True)
        assert "".join(chunk.choices[0].delta.content or "" for chunk in stream) == ANSWER
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.request.model"] == REQUEST_MODEL
    assert attrs["output.value"] == ANSWER


def test_failed_chat_records_the_requested_model(make_tracer):
    exporter = make_tracer()
    with client(unauthorized) as c, pytest.raises(openai.AuthenticationError):
        c.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}])
    span, attrs = only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert attrs["gen_ai.request.model"] == REQUEST_MODEL


def test_requested_model_survives_hidden_invocation_parameters(make_tracer):
    exporter = make_tracer(TraceConfig(hide_llm_invocation_parameters=True))
    with client(lambda _: chat_stream()) as c:
        list(c.chat.completions.create(
            model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}], stream=True))
    _, attrs = only_span(exporter)
    assert "gen_ai.request.parameters" not in attrs
    assert attrs["gen_ai.request.model"] == REQUEST_MODEL


def test_async_streamed_and_failed_chat_record_the_requested_model(make_tracer):
    exporter = make_tracer()

    async def run():
        async with async_client(lambda _: chat_stream()) as c:
            stream = await c.chat.completions.create(
                model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}], stream=True)
            async for _ in stream:
                pass
        async with async_client(unauthorized) as c:
            with pytest.raises(openai.AuthenticationError):
                await c.chat.completions.create(
                    model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}])

    asyncio.run(run())
    spans = exporter.get_finished_spans()
    assert [dict(span.attributes)["gen_ai.request.model"] for span in spans] == [
        REQUEST_MODEL, REQUEST_MODEL]


# --- gen_ai.provider.name from the client's base_url host ------------------------------


@pytest.mark.parametrize("base_url,provider", [
    ("https://api.openai.com/v1", "openai"),
    ("https://my-resource.openai.azure.com/openai/v1/", "azure"),
    ("https://my-resource.services.ai.azure.com/openai/v1/", "azure"),
    ("https://generativelanguage.googleapis.com/v1beta/openai/", "google"),
    ("https://api.deepseek.com/v1", "deepseek"),
    ("https://api.x.ai/v1", "xai"),
    ("https://api.together.ai/v1", "together"),
    ("https://API.Together.ai./v1", "together"),
    ("https://api.mistral.ai/v1", "mistralai"),
    ("https://inference.baseten.co/v1", "baseten"),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "dashscope"),
    ("https://ws1.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", "dashscope"),
    ("https://dbc-1234.cloud.databricks.com/serving-endpoints", "databricks"),
    ("https://adb-1.2.azuredatabricks.net/serving-endpoints", "databricks"),
    ("https://ws-endpoint.xenon.lepton.run/v1", "lepton"),
    ("https://api.perplexity.ai/v1", "perplexity"),
    ("https://api.orcarouter.ai/v1", "orcarouter"),
    ("https://api.anannas.ai/v1", "anannas"),
    ("https://api.doubleword.ai/v1", "doubleword"),
    ("https://API.Perplexity.ai./v1", "perplexity"),
    # Unknown hosts are not guessed: proxies, loopback servers and look-alikes stay "openai".
    ("http://127.0.0.1:8000/v1", "openai"),
    ("http://localhost:8080/v1", "openai"),
    ("https://llm-proxy.customer.example/v1", "openai"),
    ("https://inference.baseten.co.customer.example/v1", "openai"),
    ("https://notopenai.azure.com.evil.example/v1", "openai"),
    ("https://api.together.ai.customer.example/v1", "openai"),
    ("https://proxy.api.together.ai/v1", "openai"),
    ("https://notapi.together.ai/v1", "openai"),
])
def test_provider_name_follows_the_base_url_host(make_tracer, base_url, provider):
    exporter = make_tracer()
    with client(lambda _: httpx.Response(200, json=chat_completion()), base_url=base_url) as c:
        c.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}])
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == provider


def test_failed_call_keeps_the_mapped_provider(make_tracer):
    exporter = make_tracer()
    with client(unauthorized, base_url="https://api.perplexity.ai/v1") as c:
        with pytest.raises(openai.AuthenticationError):
            c.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": "q"}])
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == "perplexity"


# --- Responses API: JSON output, readable input, flat assistant message -----------------


def test_non_streamed_responses_output_is_json_with_mime_type(make_tracer):
    exporter = make_tracer()
    with client(lambda _: httpx.Response(200, json=responses_body()),
                base_url="https://api.perplexity.ai/v1") as c:
        assert c.responses.create(model=REQUEST_MODEL, input="Capital of Japan?").output_text == ANSWER
    _, attrs = only_span(exporter)
    output = json.loads(attrs["output.value"])  # valid JSON, not str(dict)
    assert output["output"][0]["content"][0]["text"] == ANSWER
    # The answer leads, so list views that preview the JSON show it first.
    assert list(output)[0] == "output_text" and output["output_text"] == ANSWER
    assert attrs["output.mime_type"] == "application/json"


def test_responses_string_input_is_not_json_quoted(make_tracer):
    exporter = make_tracer()
    with client(lambda _: httpx.Response(200, json=responses_body())) as c:
        c.responses.create(model=REQUEST_MODEL, input="Capital of Japan?")
    _, attrs = only_span(exporter)
    assert attrs["input.value"] == "Capital of Japan?"
    assert attrs.get("input.mime_type", "text/plain") == "text/plain"


@pytest.mark.parametrize("stream", [False, True], ids=["non-streamed", "streamed"])
def test_responses_assistant_text_is_a_flat_message_content(make_tracer, stream):
    exporter = make_tracer()
    handler = (lambda _: responses_stream()) if stream else (
        lambda _: httpx.Response(200, json=responses_body()))
    with client(handler) as c:
        result = c.responses.create(model=REQUEST_MODEL, input="Capital of Japan?", stream=stream)
        if stream:
            list(result)
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.output.messages.0.message.role"] == "assistant"
    assert attrs["gen_ai.output.messages.0.message.content"] == ANSWER
    assert not any(key.startswith("gen_ai.output.messages.0.message.contents") for key in attrs)


def test_responses_output_respects_hide_outputs(make_tracer):
    exporter = make_tracer(TraceConfig(hide_outputs=True))
    with client(lambda _: httpx.Response(200, json=responses_body())) as c:
        c.responses.create(model=REQUEST_MODEL, input="Capital of Japan?")
    _, attrs = only_span(exporter)
    assert attrs["output.value"] == "__REDACTED__"
    assert ANSWER not in json.dumps(attrs)


# --- Embeddings: model chip, compact output, vectors only under the hide flag -----------


def embed(exporter_config=None, make_tracer=None, **body_kwargs):
    exporter = make_tracer(exporter_config)
    with client(lambda _: httpx.Response(200, json=embeddings_body(**body_kwargs)),
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/") as c:
        response = c.embeddings.create(model=REQUEST_MODEL, input="A short example.",
                                       encoding_format="float")
    assert response.data[0].embedding == VECTOR
    return only_span(exporter)


def test_embedding_span_records_the_model(make_tracer):
    _, attrs = embed(make_tracer=make_tracer)
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert attrs["embedding.model_name"] == RESPONSE_MODEL
    assert attrs["gen_ai.provider.name"] == "google"


def test_embedding_output_is_a_compact_json_summary(make_tracer):
    _, attrs = embed(make_tracer=make_tracer)
    assert json.loads(attrs["output.value"]) == {
        "model": RESPONSE_MODEL, "embeddings": 1, "dimensions": len(VECTOR)}
    assert attrs["output.mime_type"] == "application/json"
    assert "0.25" not in attrs["output.value"]


def test_embedding_vectors_are_exported_by_default(make_tracer):
    _, attrs = embed(make_tracer=make_tracer)
    assert list(attrs["embedding.embeddings.0.embedding.vector"]) == VECTOR
    assert json.loads(attrs["embedding.embeddings"])["data"][0]["embedding"] == VECTOR
    assert attrs["embedding.embeddings.0.embedding.text"] == "A short example."


def test_hide_embedding_vectors_hides_every_copy_of_the_vectors(make_tracer):
    _, attrs = embed(TraceConfig(hide_embedding_vectors=True), make_tracer=make_tracer)
    assert "embedding.embeddings.0.embedding.vector" not in attrs
    raw = json.loads(attrs["embedding.embeddings"])
    assert "embedding" not in raw["data"][0]
    assert raw["data"][0]["dimensions"] == len(VECTOR)
    assert "0.25" not in json.dumps(attrs)


def test_embedding_items_without_index_are_not_skipped(make_tracer):
    _, attrs = embed(make_tracer=make_tracer, with_index=False)
    assert list(attrs["embedding.embeddings.0.embedding.vector"]) == VECTOR


def test_embedding_without_usage_has_no_token_attributes(make_tracer):
    _, attrs = embed(make_tracer=make_tracer, with_usage=False)
    assert not any(key.startswith(("gen_ai.usage.", "llm.token_count")) for key in attrs)


def test_embedding_with_usage_keeps_token_attributes(make_tracer):
    _, attrs = embed(make_tracer=make_tracer)
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["gen_ai.usage.total_tokens"] == 3


def test_hide_inputs_hides_embedding_text_as_well_as_input_value(make_tracer):
    _, attrs = embed(TraceConfig(hide_inputs=True), make_tracer=make_tracer)
    assert "A short example." not in json.dumps(attrs)
    assert "embedding.embeddings.0.embedding.text" not in attrs
    assert attrs["gen_ai.usage.input_tokens"] == 3


def test_hide_outputs_hides_embedding_vectors_in_all_copies(make_tracer):
    _, attrs = embed(TraceConfig(hide_outputs=True), make_tracer=make_tracer)
    assert "0.25" not in json.dumps(attrs)
    assert "embedding.embeddings.0.embedding.vector" not in attrs
    assert "embedding" not in json.loads(attrs["embedding.embeddings"])["data"][0]
    assert attrs["output.value"] == "__REDACTED__"
    assert attrs["embedding.embeddings.0.embedding.text"] == "A short example."


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
def test_together_provider_on_sync_and_async_calls(make_tracer, use_async, outcome):
    exporter = make_tracer()
    handler = {
        "success": lambda _: httpx.Response(200, json=chat_completion()),
        "stream": lambda _: chat_stream(),
        "error": unauthorized,
    }[outcome]
    kwargs = {
        "model": REQUEST_MODEL,
        "messages": [{"role": "user", "content": "q"}],
        "stream": outcome == "stream",
    }

    async def run_async():
        async with async_client(handler, base_url="https://api.together.ai/v1") as c:
            response = await c.chat.completions.create(**kwargs)
            if outcome == "stream":
                async for _ in response:
                    pass

    def run_sync():
        with client(handler, base_url="https://api.together.ai/v1") as c:
            response = c.chat.completions.create(**kwargs)
            if outcome == "stream":
                list(response)

    def run():
        if use_async:
            asyncio.run(run_async())
        else:
            run_sync()

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()

    span, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == "together"
    assert attrs["gen_ai.request.model"] == (
        RESPONSE_MODEL if outcome == "success" else REQUEST_MODEL
    )
    if outcome == "error":
        assert span.status.status_code == StatusCode.ERROR
