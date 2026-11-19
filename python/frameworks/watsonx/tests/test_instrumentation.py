"""Real ibm-watsonx-ai SDK calls with the HTTP transport stubbed: no credentials, no network."""
import asyncio
import json
import socket

import httpx
import pytest
import requests
from fi_instrumentation import TraceConfig
from ibm_watsonx_ai import APIClient, Credentials
from ibm_watsonx_ai.foundation_models import ModelInference
from ibm_watsonx_ai.foundation_models.embeddings import Embeddings
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_watsonx import WatsonxInstrumentor

LLM = "ibm/granite-3-8b-instruct"
EMB = "ibm/slate-30m-english-rtrvr"
SECRET = "sk-synthetic-secret-token"


def sse(*events):
    return "".join(
        "id: %d\nevent: message\ndata: %s\n\n" % (i + 1, json.dumps(e)) for i, e in enumerate(events)
    ).encode()


class Wire:
    """Records requests and answers with synthetic watsonx.ai responses."""

    def __init__(self):
        self.requests = []
        self.fail_with = None

    def __call__(self, req):
        url = str(req.url).split("?")[0]
        self.requests.append(req)
        if "/v2/projects/" in url:
            return httpx.Response(200, json={"entity": {"storage": {"type": "bmcos_object_storage"}}, "metadata": {"guid": "p"}})
        if url.endswith("foundation_model_specs"):
            return httpx.Response(200, json={"resources": [
                {"model_id": LLM, "functions": [{"id": "text_generation"}, {"id": "text_chat"}]},
                {"model_id": EMB, "functions": [{"id": "embedding"}]},
            ]})
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"errors": [{"code": "boom", "message": "leaked " + SECRET}]})
        if url.endswith("text/generation_stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
                {"model_id": LLM, "results": [{"generated_text": "He", "generated_token_count": 1, "input_token_count": 5, "stop_reason": "not_finished"}]},
                {"model_id": LLM, "results": [{"generated_text": "llo", "generated_token_count": 3, "input_token_count": 0, "stop_reason": "eos_token"}]},
            ))
        if url.endswith("text/chat_stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
                {"id": "c1", "model_id": LLM, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "yo"}, "finish_reason": None}]},
                {"id": "c1", "model_id": LLM, "choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}},
            ))
        if url.endswith("text/generation") and json.loads(req.content).get("input", "").startswith("echo:"):
            text = json.loads(req.content)["input"]  # per-prompt answers, to tell the prompts apart
            return httpx.Response(200, json={"model_id": LLM, "results": [
                {"generated_text": "out:" + text, "generated_token_count": len(text), "input_token_count": 4,
                 "stop_reason": "eos_token", "input_text": text}]})
        if url.endswith("text/generation"):
            return httpx.Response(200, json={"model_id": LLM, "results": [
                {"generated_text": "Tokyo", "generated_token_count": 3, "input_token_count": 5, "stop_reason": "eos_token"}]})
        if url.endswith("text/chat"):
            return httpx.Response(200, json={"id": "c1", "model_id": LLM, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "Tokyo"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})
        if url.endswith("text/embeddings"):
            n = len(json.loads(req.content)["inputs"])
            return httpx.Response(200, json={"model_id": EMB, "input_token_count": 7,
                                             "results": [{"embedding": [0.25, 0.5, 0.75]}] * n})
        return httpx.Response(404, json={"errors": [{"code": "nf", "message": url}]})


def _as_requests_response(response, request):
    out = requests.Response()
    out.status_code = response.status_code
    out._content = response.content
    out.headers.update(response.headers)
    out.url = request.url
    out.request = request
    return out


@pytest.fixture
def wire(monkeypatch):
    """Route every HTTP library the SDK uses (httpx, and requests in older SDKs) to the stub."""
    wire = Wire()
    for cls in (httpx.Client, httpx.AsyncClient):
        original = cls.__init__

        def init(self, *a, __orig=original, **k):
            k["transport"] = httpx.MockTransport(wire)
            __orig(self, *a, **k)

        monkeypatch.setattr(cls, "__init__", init)

    def send(self, request, **kwargs):
        body = request.body.encode() if isinstance(request.body, str) else (request.body or b"")
        stubbed = wire(httpx.Request(request.method, request.url, content=body))
        return _as_requests_response(stubbed, request)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)

    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    return wire


@pytest.fixture
def api(wire):
    return APIClient(credentials=Credentials(url="https://us-south.ml.cloud.ibm.com", token=SECRET), project_id="p")


def make_spans(config=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = WatsonxInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **({"config": config} if config else {}))
    return exporter, provider, instrumentor


@pytest.fixture
def spans():
    exporter, provider, instrumentor = make_spans()
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


@pytest.fixture
def model(api):
    return ModelInference(model_id=LLM, api_client=api)


def one(spans):
    finished = spans.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0], dict(finished[0].attributes)


def test_generate_records_llm_span(spans, model):
    result = model.generate(prompt="Capital of Japan?", params={"max_new_tokens": 20, "temperature": 0.5, "top_p": 0.9})
    assert result["results"][0]["generated_text"] == "Tokyo"
    span, a = one(spans)
    assert a["gen_ai.span.kind"] == "LLM"
    assert a["gen_ai.provider.name"] == "ibm.watsonx.ai"
    assert a["gen_ai.operation.name"] == "text_completion"
    assert a["gen_ai.request.model"] == LLM
    assert a["gen_ai.request.max_tokens"] == 20
    assert a["gen_ai.request.temperature"] == 0.5
    assert a["gen_ai.request.top_p"] == 0.9
    assert json.loads(a["gen_ai.request.parameters"])["max_new_tokens"] == 20
    assert a["gen_ai.response.model"] == LLM
    assert a["gen_ai.usage.input_tokens"] == 5
    assert a["gen_ai.usage.output_tokens"] == 3
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("eos_token",)
    assert a["gen_ai.input.messages.0.message.role"] == "user"
    assert a["gen_ai.input.messages.0.message.content"] == "Capital of Japan?"
    assert a["gen_ai.output.messages.0.message.role"] == "assistant"
    assert a["gen_ai.output.messages.0.message.content"] == "Tokyo"
    assert span.status.status_code != StatusCode.ERROR


def test_generate_text_makes_exactly_one_span(spans, model):
    assert model.generate_text(prompt="Capital of Japan?") == "Tokyo"
    span, a = one(spans)  # generate_text calls generate internally: no nested duplicate
    assert a["gen_ai.output.messages.0.message.content"] == "Tokyo"


def test_generate_text_parameter_object(spans, model):
    from ibm_watsonx_ai.foundation_models.schema import TextGenParameters

    model.generate_text(prompt="x", params=TextGenParameters(max_new_tokens=7, temperature=0.1))
    _, a = one(spans)
    assert a["gen_ai.request.max_tokens"] == 7
    assert a["gen_ai.request.temperature"] == 0.1


def test_chat_records_messages_usage_and_finish(spans, model):
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital of Japan?"}]
    out = model.chat(messages=messages, params={"max_tokens": 9, "temperature": 0.2})
    assert out["choices"][0]["message"]["content"] == "Tokyo"
    _, a = one(spans)
    assert a["gen_ai.span.kind"] == "LLM"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.model"] == LLM
    assert a["gen_ai.request.max_tokens"] == 9
    assert a["gen_ai.input.messages.0.message.role"] == "system"
    assert a["gen_ai.input.messages.1.message.content"] == "Capital of Japan?"
    assert a["gen_ai.output.messages.0.message.content"] == "Tokyo"
    assert a["gen_ai.response.id"] == "c1"
    assert a["gen_ai.usage.input_tokens"] == 4
    assert a["gen_ai.usage.output_tokens"] == 2
    assert a["gen_ai.usage.total_tokens"] == 6
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("stop",)


def test_generate_text_stream_assembles_output_and_ends_on_exhaustion(spans, model):
    stream = model.generate_text_stream(prompt="Hi")
    assert spans.get_finished_spans() == ()
    assert list(stream) == ["He", "llo"]
    _, a = one(spans)
    assert a["gen_ai.output.messages.0.message.content"] == "Hello"
    assert "gen_ai.usage.input_tokens" not in a  # text-only chunks carry no counts
    assert a["gen_ai.input.messages.0.message.content"] == "Hi"


def test_generate_text_stream_raw_chunks_pass_through(spans, model):
    chunks = list(model.generate_text_stream(prompt="Hi", raw_response=True))
    assert [c["results"][0]["generated_text"] for c in chunks] == ["He", "llo"]
    _, a = one(spans)
    assert a["gen_ai.output.messages.0.message.content"] == "Hello"
    assert a["gen_ai.usage.input_tokens"] == 5
    assert a["gen_ai.usage.output_tokens"] == 3
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("eos_token",)


def test_chat_stream_assembles_output_usage(spans, model):
    chunks = list(model.chat_stream(messages=[{"role": "user", "content": "hi"}]))
    assert len(chunks) == 2
    _, a = one(spans)
    assert a["gen_ai.output.messages.0.message.content"] == "yo!"
    assert a["gen_ai.output.messages.0.message.role"] == "assistant"
    assert a["gen_ai.usage.input_tokens"] == 4
    assert a["gen_ai.usage.output_tokens"] == 2
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("stop",)


def test_abandoned_stream_ends_span_on_close_without_output(spans, model):
    stream = model.chat_stream(messages=[{"role": "user", "content": "hi"}])
    next(stream)
    assert spans.get_finished_spans() == ()
    stream.close()
    span, a = one(spans)
    assert a["watsonx.stream.incomplete"] is True
    assert "gen_ai.output.messages.0.message.content" not in a
    assert span.status.status_code != StatusCode.ERROR


def test_embeddings_documents_and_query(spans, api):
    emb = Embeddings(model_id=EMB, api_client=api)
    assert emb.embed_documents(["alpha", "beta"]) == [[0.25, 0.5, 0.75]] * 2
    assert emb.embed_query("gamma") == [0.25, 0.5, 0.75]
    finished = spans.get_finished_spans()
    assert len(finished) == 2  # embed_* call generate internally: no nested duplicates
    docs, query = (dict(s.attributes) for s in finished)
    assert docs["gen_ai.span.kind"] == "EMBEDDING"
    assert docs["gen_ai.provider.name"] == "ibm.watsonx.ai"
    assert docs["gen_ai.operation.name"] == "embeddings"
    assert docs["gen_ai.request.model"] == EMB
    assert docs["embedding.embeddings.0.embedding.text"] == "alpha"
    assert docs["embedding.embeddings.1.embedding.text"] == "beta"
    assert docs["gen_ai.embeddings.dimension.count"] == 3
    assert query["embedding.embeddings.0.embedding.text"] == "gamma"
    for attrs in (docs, query):  # vectors are never exported by default
        assert not any("vector" in k for k in attrs)
        assert "0.25" not in json.dumps(attrs, default=str)


def test_error_sets_status_error_type_without_message_and_reraises(spans, model, wire):
    from ibm_watsonx_ai.wml_client_error import ApiRequestFailure

    wire.fail_with = 500
    with pytest.raises(ApiRequestFailure):
        model.chat(messages=[{"role": "user", "content": "hi"}])
    span, a = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert a["error.type"] == "ApiRequestFailure"
    assert SECRET not in json.dumps(a, default=str)
    assert not span.events  # no recorded exception message or stack


def test_stream_error_ends_span_with_error(spans, model, wire):
    from ibm_watsonx_ai.wml_client_error import WMLClientError

    wire.fail_with = 500
    with pytest.raises(WMLClientError):
        list(model.generate_text_stream(prompt="Hi"))
    span, a = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert a["error.type"] == "WMLClientError"
    assert SECRET not in json.dumps(a, default=str)


def test_async_variants(spans, model, api):
    emb = Embeddings(model_id=EMB, api_client=api)
    async_embeddings = hasattr(Embeddings, "aembed_query")  # absent in the oldest supported SDK

    async def run():
        chat = await model.achat(messages=[{"role": "user", "content": "hi"}])
        stream = await model.achat_stream(messages=[{"role": "user", "content": "hi"}])
        chunks = [c async for c in stream]
        gen = await model.agenerate(prompt="x")
        gstream = await model.agenerate_stream(prompt="x")
        gchunks = [c async for c in gstream]
        vectors = (await emb.aembed_query("q"), await emb.aembed_documents(["a"])) if async_embeddings else None
        return chat, chunks, gen, gchunks, vectors

    chat, chunks, gen, gchunks, vectors = asyncio.run(run())
    assert chat["choices"][0]["message"]["content"] == "Tokyo" and len(chunks) == 2 and len(gchunks) == 2
    assert gen["results"][0]["generated_text"] == "Tokyo"
    finished = [dict(s.attributes) for s in spans.get_finished_spans()]
    assert len(finished) == (6 if async_embeddings else 4), [a["gen_ai.operation.name"] for a in finished]
    if async_embeddings:
        assert vectors == ([0.25, 0.5, 0.75], [[0.25, 0.5, 0.75]])
        assert sum(a["gen_ai.span.kind"] == "EMBEDDING" for a in finished) == 2
    outs = sorted(
        a["gen_ai.output.messages.0.message.content"] for a in finished
        if "gen_ai.output.messages.0.message.content" in a
    )
    assert outs == ["Hello", "Tokyo", "Tokyo", "yo!"]


def test_hidden_content_removes_prompts_outputs_but_keeps_metadata(api, wire):
    exporter, provider, instrumentor = make_spans(TraceConfig(hide_inputs=True, hide_outputs=True,
                                                              hide_input_messages=True, hide_output_messages=True,
                                                              hide_input_text=True, hide_output_text=True))
    try:
        model = ModelInference(model_id=LLM, api_client=api)
        model.chat(messages=[{"role": "user", "content": "my private question"}])
        list(model.generate_text_stream(prompt="my private prompt"))
        Embeddings(model_id=EMB, api_client=api).embed_query("my private text")
        for span in exporter.get_finished_spans():
            dumped = json.dumps(dict(span.attributes), default=str)
            assert "private" not in dumped and "Tokyo" not in dumped and "Hello" not in dumped and "yo!" not in dumped
            assert span.attributes["gen_ai.request.model"] in (LLM, EMB)
        chat = [s for s in exporter.get_finished_spans() if s.attributes.get("gen_ai.operation.name") == "chat"][0]
        assert chat.attributes["gen_ai.usage.input_tokens"] == 4
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_credentials_never_exported(spans, model):
    model.chat(messages=[{"role": "user", "content": "hi"}])
    _, a = one(spans)
    assert SECRET not in json.dumps(a, default=str)


def test_uninstrument_restores_originals_and_stops_tracing(api):
    before = ModelInference.chat
    exporter, provider, instrumentor = make_spans()
    assert ModelInference.chat is not before
    instrumentor.uninstrument()
    provider.shutdown()
    assert ModelInference.chat is before
    ModelInference(model_id=LLM, api_client=api).chat(messages=[{"role": "user", "content": "hi"}])
    assert exporter.get_finished_spans() == ()


# ---- round 2 regressions -------------------------------------------------------------------------


def test_generate_text_records_usage_finish_reason_and_response_model(spans, model):
    assert model.generate_text(prompt="Capital of Japan?") == "Tokyo"
    _, a = one(spans)
    assert a["gen_ai.usage.input_tokens"] == 5
    assert a["gen_ai.usage.output_tokens"] == 3
    assert a["gen_ai.usage.total_tokens"] == 8
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("eos_token",)
    assert a["gen_ai.response.model"] == LLM


def test_embed_query_and_documents_record_usage_and_dimension(spans, api):
    emb = Embeddings(model_id=EMB, api_client=api)
    emb.embed_documents(["alpha", "beta"])
    emb.embed_query("gamma")
    finished = [dict(s.attributes) for s in spans.get_finished_spans()]
    assert len(finished) == 2
    for a in finished:
        assert a["gen_ai.usage.input_tokens"] == 7
        assert a["gen_ai.embeddings.dimension.count"] == 3
        assert a["gen_ai.response.model"] == EMB


def test_instance_level_params_are_recorded_and_call_params_win(spans, api):
    model = ModelInference(model_id=LLM, api_client=api, params={"max_new_tokens": 11, "temperature": 0.3})
    model.generate_text(prompt="x")
    model.generate_text(prompt="x", params={"temperature": 0.9})
    first, second = (dict(s.attributes) for s in spans.get_finished_spans())
    assert first["gen_ai.request.max_tokens"] == 11 and first["gen_ai.request.temperature"] == 0.3
    assert json.loads(first["gen_ai.request.parameters"]) == {"max_new_tokens": 11, "temperature": 0.3}
    assert second["gen_ai.request.max_tokens"] == 11 and second["gen_ai.request.temperature"] == 0.9


def test_instance_level_chat_and_embedding_params(spans, api):
    ModelInference(model_id=LLM, api_client=api, params={"max_tokens": 5, "top_p": 0.4}).chat(
        messages=[{"role": "user", "content": "hi"}])
    Embeddings(model_id=EMB, api_client=api, params={"truncate_input_tokens": 3}).embed_query("q")
    chat, emb = (dict(s.attributes) for s in spans.get_finished_spans())
    assert chat["gen_ai.request.max_tokens"] == 5 and chat["gen_ai.request.top_p"] == 0.4
    assert json.loads(emb["gen_ai.request.parameters"]) == {"truncate_input_tokens": 3}


def test_prompt_variables_are_not_request_parameters(spans, model):
    model.generate_text(prompt="x", params={"max_new_tokens": 4, "prompt_variables": {"name": "SECRETVAL"}})
    _, a = one(spans)
    assert "SECRETVAL" not in a["gen_ai.request.parameters"]
    assert json.loads(a["gen_ai.request.parameters"]) == {"max_new_tokens": 4}
    assert json.loads(a["gen_ai.prompt.template.variables"]) == {"name": "SECRETVAL"}


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_messages", "hide_input_text"])
def test_prompt_variables_follow_the_input_hiding_flags(api, wire, flag):
    exporter, provider, instrumentor = make_spans(TraceConfig(**{flag: True}))
    try:
        ModelInference(model_id=LLM, api_client=api).generate_text(
            prompt="x", params={"max_new_tokens": 4, "prompt_variables": {"name": "SECRETVAL"}})
        (span,) = exporter.get_finished_spans()
        assert "SECRETVAL" not in json.dumps(dict(span.attributes), default=str)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_input_and_output_value_are_set_like_sibling_providers(spans, model):
    model.chat(messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital?"}])
    model.generate_text(prompt="Capital of Japan?")
    list(model.generate_text_stream(prompt="Hi"))
    chat, text, stream = (dict(s.attributes) for s in spans.get_finished_spans())
    assert json.loads(chat["input.value"]) == [
        {"role": "system", "content": "Be brief."}, {"role": "user", "content": "Capital?"}]
    assert chat["input.mime_type"] == "application/json"
    assert chat["output.value"] == "Tokyo"
    assert json.loads(text["input.value"]) == "Capital of Japan?" and text["output.value"] == "Tokyo"
    assert stream["output.value"] == "Hello"


def test_input_output_value_never_include_images_or_embedding_vectors(spans, model, api):
    model.chat(messages=[{"role": "user", "content": [
        {"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}])
    Embeddings(model_id=EMB, api_client=api).embed_query("q")
    chat, emb = (dict(s.attributes) for s in spans.get_finished_spans())
    assert "input.value" not in chat or "AAAA" not in chat["input.value"]
    assert "AAAA" not in json.dumps(chat, default=str)
    assert "input.value" not in emb and "output.value" not in emb


@pytest.mark.parametrize("flags", [
    {"hide_inputs": True, "hide_outputs": True},
    {"hide_input_messages": True, "hide_output_messages": True},
    {"hide_input_text": True, "hide_output_text": True},
])
def test_input_output_value_follow_the_hiding_flags(api, wire, flags):
    exporter, provider, instrumentor = make_spans(TraceConfig(**flags))
    try:
        model = ModelInference(model_id=LLM, api_client=api)
        model.chat(messages=[{"role": "user", "content": "my private question"}])
        model.generate_text(prompt="my private prompt")
        for span in exporter.get_finished_spans():
            dumped = json.dumps(dict(span.attributes), default=str)
            assert "private" not in dumped and "Tokyo" not in dumped
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_tracing_failure_in_start_never_breaks_the_call(model, monkeypatch):
    import traceai_watsonx

    exporter, provider, instrumentor = make_spans()
    try:
        def boom(*a, **k):
            raise RuntimeError("tracing bug")

        monkeypatch.setattr(traceai_watsonx, "_start", boom)
        assert model.generate_text(prompt="x") == "Tokyo"
        assert model.chat(messages=[{"role": "user", "content": "hi"}])["choices"][0]["message"]["content"] == "Tokyo"
        assert exporter.get_finished_spans() == ()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_tracing_failure_in_result_handling_never_breaks_the_call(model, monkeypatch):
    import traceai_watsonx

    exporter, provider, instrumentor = make_spans()
    try:
        def boom(*a, **k):
            raise RuntimeError("tracing bug")

        monkeypatch.setattr(traceai_watsonx._Output, "feed", boom)
        monkeypatch.setattr(traceai_watsonx._Output, "apply", boom)
        assert model.generate_text(prompt="x") == "Tokyo"
        assert model.chat(messages=[{"role": "user", "content": "hi"}])["choices"][0]["message"]["content"] == "Tokyo"
        assert len(exporter.get_finished_spans()) == 2  # spans still end
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("async_mode", [False, True])
def test_prompt_list_outputs_are_indexed_per_prompt_and_usage_is_summed(spans, model, async_mode):
    prompts = ["echo:aaa", "echo:bbbbb"]
    result = model.generate(prompt=prompts, async_mode=async_mode)
    list(result) if async_mode else result
    _, a = one(spans)
    assert a["gen_ai.output.messages.0.message.content"] == "out:echo:aaa"
    assert a["gen_ai.output.messages.1.message.content"] == "out:echo:bbbbb"
    assert a["gen_ai.input.messages.1.message.content"] == "echo:bbbbb"
    assert a["gen_ai.usage.input_tokens"] == 8
    assert a["gen_ai.usage.output_tokens"] == len("echo:aaa") + len("echo:bbbbb")
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("eos_token", "eos_token")
