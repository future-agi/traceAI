"""Writer SDK instrumentation over the real SDK client with a stubbed HTTP transport."""
import asyncio
import json

import httpx
import pytest
import writerai
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import StatusCode

from conftest import (
    API_KEY, attrs, blob, chat_body, chunk, completion_body, make_async_client, make_client, sse,
)
from fi_instrumentation import TraceConfig

MESSAGES = [{"role": "user", "content": "Capital of Japan?"}]
OUT = "gen_ai.output.messages.0.message"


def one(spans):
    finished = spans.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0]


def ok_handler(body):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    handler.seen = seen
    return handler


# --- chat --------------------------------------------------------------------------------------

def test_chat_records_model_parameters_usage_and_content(spans):
    handler = ok_handler(chat_body())
    client = make_client(handler)
    result = client.chat.chat(
        messages=MESSAGES, model="palmyra-x5", temperature=0.2, max_tokens=16, top_p=0.9, stop=["END"]
    )
    assert result.choices[0].message.content == "Tokyo"
    # The SDK request is untouched.
    sent = json.loads(handler.seen[0].content)
    assert sent["messages"] == MESSAGES and sent["model"] == "palmyra-x5"
    span = one(spans)
    a = attrs(span)
    assert span.name == "writer.chat"
    assert a["gen_ai.span.kind"] == "LLM"
    assert a["gen_ai.provider.name"] == "writer"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.model"] == "palmyra-x5"
    assert a["gen_ai.request.temperature"] == 0.2
    assert a["gen_ai.request.max_tokens"] == 16
    assert a["gen_ai.request.top_p"] == 0.9
    assert list(a["gen_ai.request.stop_sequences"]) == ["END"]
    assert a["gen_ai.response.model"] == "palmyra-x5-served"
    assert a["gen_ai.response.id"] == "chat-1"
    assert list(a["gen_ai.response.finish_reasons"]) == ["stop"]
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (3, 2, 5)
    assert json.loads(a["input.value"]) == MESSAGES
    assert a["output.value"] == "Tokyo"
    assert API_KEY not in blob(span)
    assert span.status.status_code != StatusCode.ERROR


def test_chat_async(spans):
    client = make_async_client(ok_handler(chat_body()))

    async def go():
        return await client.chat.chat(messages=MESSAGES, model="palmyra-x5")

    assert asyncio.run(go()).choices[0].message.content == "Tokyo"
    a = attrs(one(spans))
    assert a["output.value"] == "Tokyo" and a["gen_ai.usage.total_tokens"] == 5


def test_tool_calls_in_response_are_recorded(spans):
    body = chat_body(
        "",
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'}}],
    )
    body["choices"][0]["finish_reason"] = "tool_calls"
    client = make_client(ok_handler(body))
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    client.chat.chat(messages=MESSAGES, model="palmyra-x5", tools=tools)
    a = attrs(one(spans))
    prefix = f"{OUT}.tool_calls.0.tool_call"
    assert a[f"{prefix}.id"] == "call-1"
    assert a[f"{prefix}.function.name"] == "get_weather"
    assert json.loads(a[f"{prefix}.function.arguments"]) == {"city": "Tokyo"}
    assert list(a["gen_ai.response.finish_reasons"]) == ["tool_calls"]


def test_tool_arguments_are_hidden_with_outputs(instrument):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    body = chat_body("", tool_calls=[{"id": "c", "type": "function", "function": {"name": "lookup", "arguments": '{"ssn": "SECRET-ARG"}'}}])
    make_client(ok_handler(body)).chat.chat(messages=MESSAGES, model="m")
    span = one(spans)
    assert "SECRET-ARG" not in blob(span)
    assert attrs(span)["gen_ai.usage.total_tokens"] == 5


def test_tool_result_messages_are_part_of_the_input(spans):
    messages = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "w", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
    ]
    make_client(ok_handler(chat_body("It is sunny"))).chat.chat(messages=messages, model="m")
    a = attrs(one(spans))
    parsed = json.loads(a["input.value"])
    assert [m["role"] for m in parsed] == ["user", "assistant", "tool"]
    assert parsed[2]["content"] == "sunny"


def test_graph_documents_and_web_sources_are_not_exported(spans):
    body = chat_body(
        "answer",
        graph_data={"sources": [{"file_id": "f1", "score": 0.9, "text": "CONFIDENTIAL-DOCUMENT-BODY"}], "status": "finished"},
        web_search_data={"sources": [{"url": "http://x", "raw_content": "CONFIDENTIAL-WEB-BODY"}]},
    )
    make_client(ok_handler(body)).chat.chat(messages=MESSAGES, model="m", tools=[{"type": "graph", "function": {"graph_ids": ["g1"], "subqueries": True}}])
    span = one(spans)
    assert "CONFIDENTIAL" not in blob(span)
    assert attrs(span)["output.value"] == "answer"


def test_multimodal_or_nonstring_message_content_is_not_serialized(spans):
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRETPIXELS"}}]}]
    make_client(ok_handler(chat_body())).chat.chat(messages=messages, model="m")
    span = one(spans)
    assert "SECRETPIXELS" not in blob(span)
    assert "input.value" not in attrs(span)


# --- streaming ---------------------------------------------------------------------------------

def stream_handler():
    return lambda request: sse(
        chunk("Tok"),
        chunk("yo"),
        chunk(None, finish="stop", usage={"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}),
    )


def test_stream_ends_on_exhaustion_with_assembled_output(spans):
    client = make_client(stream_handler())
    stream = client.chat.chat(messages=MESSAGES, model="palmyra-x5", stream=True, stream_options={"include_usage": True})
    assert spans.get_finished_spans() == ()
    pieces = [c.choices[0].delta.content for c in stream]
    assert pieces[:2] == ["Tok", "yo"]
    span = one(spans)
    a = attrs(span)
    assert a["output.value"] == "Tokyo"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (6, 2, 8)
    assert list(a["gen_ai.response.finish_reasons"]) == ["stop"]
    assert a["gen_ai.response.model"] == "palmyra-x5-served"


def test_stream_async(spans):
    client = make_async_client(stream_handler())

    async def go():
        stream = await client.chat.chat(messages=MESSAGES, model="m", stream=True)
        return [c.choices[0].delta.content async for c in stream]

    assert asyncio.run(go())[:2] == ["Tok", "yo"]
    assert attrs(one(spans))["output.value"] == "Tokyo"


def test_stream_close_ends_span_incomplete_once(spans):
    stream = make_client(stream_handler()).chat.chat(messages=MESSAGES, model="m", stream=True)
    next(iter(stream))
    assert spans.get_finished_spans() == ()
    stream.close()
    span = one(spans)
    assert attrs(span)["writer.stream.incomplete"] is True
    stream.close()
    assert len(spans.get_finished_spans()) == 1


def test_stream_context_manager(spans):
    with make_client(stream_handler()).chat.chat(messages=MESSAGES, model="m", stream=True) as stream:
        for _ in stream:
            pass
    assert len(spans.get_finished_spans()) == 1


def test_streamed_tool_call_deltas_are_assembled(spans):
    handler = lambda request: sse(
        chunk(None, tool_calls=[{"index": 0, "id": "call-9", "type": "function", "function": {"name": "get_weather", "arguments": '{"ci'}}]),
        chunk(None, tool_calls=[{"index": 0, "function": {"arguments": 'ty": "Oslo"}'}}]),
        chunk(None, finish="tool_calls"),
    )
    for _ in make_client(handler).chat.chat(messages=MESSAGES, model="m", stream=True):
        pass
    a = attrs(one(spans))
    prefix = f"{OUT}.tool_calls.0.tool_call"
    assert a[f"{prefix}.id"] == "call-9"
    assert a[f"{prefix}.function.name"] == "get_weather"
    assert json.loads(a[f"{prefix}.function.arguments"]) == {"city": "Oslo"}


def test_chat_stream_helper_produces_exactly_one_span(spans):
    if not hasattr(writerai.resources.chat.ChatResource, "stream"):
        pytest.skip("chat.stream() helper was added after writer-sdk 2.0.0")
    with make_client(stream_handler()).chat.stream(messages=MESSAGES, model="palmyra-x5") as stream:
        for _ in stream:
            pass
    span = one(spans)
    assert span.name == "writer.chat"
    assert attrs(span)["output.value"] == "Tokyo"


def test_stream_hidden_outputs(instrument):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    for _ in make_client(stream_handler()).chat.chat(messages=MESSAGES, model="m", stream=True):
        pass
    span = one(spans)
    assert "Tokyo" not in blob(span)
    assert attrs(span)["gen_ai.usage.total_tokens"] == 8


def test_stream_error_midway(spans):
    class Boom(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: ' + json.dumps(chunk("a")).encode() + b"\n\n"
            raise httpx.ReadError("connection lost SECRETDETAIL")

    handler = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Boom())
    stream = make_client(handler).chat.chat(messages=MESSAGES, model="m", stream=True)
    with pytest.raises(httpx.ReadError):
        for _ in stream:
            pass
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "ReadError"
    assert "SECRETDETAIL" not in blob(span)


def test_stream_error_midway_keeps_what_was_received(instrument):
    class Boom(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: ' + json.dumps(chunk("Tok")).encode() + b"\n\n"
            raise httpx.ReadError("connection lost SECRETDETAIL")

    handler = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Boom())

    def run(spans):
        with pytest.raises(httpx.ReadError):
            for _ in make_client(handler).chat.chat(messages=MESSAGES, model="m", stream=True):
                pass
        return one(spans)

    span = run(instrument())
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "ReadError"
    assert attrs(span)["output.value"] == "Tok"
    assert attrs(span)["gen_ai.response.model"] == "palmyra-x5-served"
    assert attrs(span)["writer.stream.incomplete"] is True
    assert "SECRETDETAIL" not in blob(span)


def test_stream_error_midway_respects_hidden_outputs(instrument):
    class Boom(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: ' + json.dumps(chunk("Tok")).encode() + b"\n\n"
            raise httpx.ReadError("lost")

    handler = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Boom())
    spans = instrument(config=TraceConfig(hide_outputs=True))
    with pytest.raises(httpx.ReadError):
        for _ in make_client(handler).chat.chat(messages=MESSAGES, model="m", stream=True):
            pass
    span = one(spans)
    assert "Tok" not in blob(span)
    assert attrs(span)["error.type"] == "ReadError"


# --- completions -------------------------------------------------------------------------------

def test_completions_sync_async_and_stream(spans):
    handler = ok_handler(completion_body("done"))
    client = make_client(handler)
    client.completions.create(model="palmyra-x-003-instruct", prompt="Say done", max_tokens=5, temperature=0.1)
    span = spans.get_finished_spans()[0]
    a = attrs(span)
    assert span.name == "writer.text_completion"
    assert a["gen_ai.span.kind"] == "LLM" and a["gen_ai.provider.name"] == "writer"
    assert a["gen_ai.request.model"] == "palmyra-x-003-instruct"
    assert a["gen_ai.request.max_tokens"] == 5
    assert a["input.value"] == "Say done"
    assert a["output.value"] == "done"

    spans.clear()
    aclient = make_async_client(ok_handler(completion_body("async done")))
    asyncio.run(aclient.completions.create(model="m", prompt="p"))
    assert attrs(one(spans))["output.value"] == "async done"

    spans.clear()
    stream_h = lambda request: sse({"value": "he"}, {"value": "llo"})
    for _ in make_client(stream_h).completions.create(model="m", prompt="p", stream=True):
        pass
    assert attrs(one(spans))["output.value"] == "hello"


# --- errors ------------------------------------------------------------------------------------

@pytest.mark.parametrize("status, error_class", [(429, "RateLimitError"), (500, "InternalServerError"), (401, "AuthenticationError")])
def test_status_errors_are_reraised_unchanged(spans, status, error_class):
    handler = lambda request: httpx.Response(status, json={"message": "failure with SECRETDETAIL"})
    with pytest.raises(writerai.APIStatusError) as raised:
        make_client(handler).chat.chat(messages=MESSAGES, model="m")
    assert type(raised.value).__name__ == error_class
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == error_class
    assert "SECRETDETAIL" not in blob(span)
    assert span.end_time is not None


def test_connection_error_and_async_error(spans):
    def boom(request):
        raise httpx.ConnectError("no route SECRETDETAIL")

    with pytest.raises(writerai.APIConnectionError):
        make_client(boom).completions.create(model="m", prompt="p")
    assert attrs(one(spans))["error.type"] == "APIConnectionError"

    spans.clear()
    handler = lambda request: httpx.Response(500, json={"message": "x"})
    with pytest.raises(writerai.InternalServerError):
        asyncio.run(make_async_client(handler).chat.chat(messages=MESSAGES, model="m"))
    assert attrs(one(spans))["error.type"] == "InternalServerError"


def test_invalid_arguments_still_raise_sdk_errors(spans):
    with pytest.raises(TypeError):
        make_client(ok_handler(chat_body())).chat.chat(messages=MESSAGES)  # model is required
    assert one(spans).status.status_code == StatusCode.ERROR


# --- privacy -----------------------------------------------------------------------------------

@pytest.mark.parametrize("config, hidden, visible", [
    (TraceConfig(hide_inputs=True), "TOPSECRET-IN", "TOPSECRET-OUT"),
    (TraceConfig(hide_outputs=True), "TOPSECRET-OUT", "TOPSECRET-IN"),
    (TraceConfig(hide_input_messages=True), "TOPSECRET-IN", "TOPSECRET-OUT"),
    (TraceConfig(hide_output_text=True), "TOPSECRET-OUT", "TOPSECRET-IN"),
])
def test_hidden_content_is_not_exported(instrument, config, hidden, visible):
    spans = instrument(config=config)
    make_client(ok_handler(chat_body("TOPSECRET-OUT"))).chat.chat(
        messages=[{"role": "user", "content": "TOPSECRET-IN"}], model="m"
    )
    span = one(spans)
    assert hidden not in blob(span)
    assert visible in blob(span)
    assert attrs(span)["gen_ai.request.model"] == "m"
    assert attrs(span)["gen_ai.usage.total_tokens"] == 5


def test_hidden_completion_prompt(instrument):
    spans = instrument(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    make_client(ok_handler(completion_body("TOPSECRET-OUT"))).completions.create(model="m", prompt="TOPSECRET-IN")
    assert "TOPSECRET" not in blob(one(spans))


# --- outermost call only / lifecycle -----------------------------------------------------------

def test_raw_and_streaming_response_variants_trace_once_and_still_parse(spans):
    client = make_client(ok_handler(chat_body()))
    raw = client.chat.with_raw_response.chat(messages=MESSAGES, model="m")
    assert raw.parse().choices[0].message.content == "Tokyo"
    assert len(spans.get_finished_spans()) == 1
    spans.clear()
    with client.chat.with_streaming_response.chat(messages=MESSAGES, model="m") as response:
        assert response.parse().choices[0].message.content == "Tokyo"
    assert len(spans.get_finished_spans()) == 1


def test_suppressed_instrumentation_creates_no_span(spans):
    token = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        make_client(ok_handler(chat_body())).chat.chat(messages=MESSAGES, model="m")
    finally:
        context_api.detach(token)
    assert spans.get_finished_spans() == ()


def test_uninstrument_restores_the_sdk_and_can_reinstrument(provider):
    from traceai_writer import WriterInstrumentor

    original = writerai.resources.chat.ChatResource.__dict__["chat"]
    instrumentor = WriterInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    assert writerai.resources.chat.ChatResource.__dict__["chat"] is not original
    instrumentor.uninstrument()
    assert writerai.resources.chat.ChatResource.__dict__["chat"] is original
    make_client(ok_handler(chat_body())).chat.chat(messages=MESSAGES, model="m")
    assert provider.exporter.get_finished_spans() == ()
    instrumentor.instrument(tracer_provider=provider)
    make_client(ok_handler(chat_body())).chat.chat(messages=MESSAGES, model="m")
    instrumentor.uninstrument()
    assert len(provider.exporter.get_finished_spans()) == 1


def test_dependency_declaration():
    from traceai_writer import WriterInstrumentor, __version__

    assert any(dep.startswith("writer-sdk") for dep in WriterInstrumentor().instrumentation_dependencies())
    assert __version__


# --- early exit from the chat.stream() helper --------------------------------------------------

def _need_helper():
    if not hasattr(writerai.resources.chat.ChatResource, "stream"):
        pytest.skip("chat.stream() helper was added after writer-sdk 2.0.0")


def test_chat_stream_helper_left_early_ends_its_span_once(spans):
    _need_helper()
    with make_client(stream_handler()).chat.stream(messages=MESSAGES, model="palmyra-x5") as stream:
        for _event in stream:
            break
    span = one(spans)
    assert span.name == "writer.chat"
    assert attrs(span)["writer.stream.incomplete"] is True


def test_chat_stream_helper_explicit_close_ends_its_span_once(spans):
    _need_helper()
    manager = make_client(stream_handler()).chat.stream(messages=MESSAGES, model="m")
    stream = manager.__enter__()
    next(iter(stream))
    assert spans.get_finished_spans() == ()
    stream.close()
    stream.close()
    manager.__exit__(None, None, None)
    assert attrs(one(spans))["writer.stream.incomplete"] is True


def test_chat_stream_helper_read_to_the_end_is_not_incomplete(spans):
    _need_helper()
    with make_client(stream_handler()).chat.stream(messages=MESSAGES, model="m") as stream:
        for _event in stream:
            pass
    span = one(spans)
    assert "writer.stream.incomplete" not in attrs(span)
    assert attrs(span)["output.value"] == "Tokyo"


def test_async_chat_stream_helper_left_early_ends_its_span_once(spans):
    _need_helper()
    client = make_async_client(stream_handler())

    async def go():
        async with client.chat.stream(messages=MESSAGES, model="palmyra-x5") as stream:
            async for _event in stream:
                break

    asyncio.run(go())
    span = one(spans)
    assert attrs(span)["writer.stream.incomplete"] is True


def test_abandoned_stream_is_not_exported_from_garbage_collection(spans):
    """A finalizer must not export a span: it can run at any point, including interpreter exit."""
    import gc

    stream = make_client(stream_handler()).chat.chat(messages=MESSAGES, model="m", stream=True)
    next(iter(stream))
    assert spans.get_finished_spans() == ()
    del stream
    gc.collect()
    assert spans.get_finished_spans() == ()


def test_abandoned_stream_exports_nothing_at_interpreter_exit():
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import httpx, json
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
        from traceai_writer import WriterInstrumentor
        from writerai import Writer

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        WriterInstrumentor().instrument(tracer_provider=provider)
        body = "".join("data: " + json.dumps({"id": "c", "model": "m", "choices": [
            {"index": 0, "delta": {"content": t}, "finish_reason": None}]}) + "\\n\\n" for t in "ab")
        client = Writer(api_key="k", base_url="http://writer.test", max_retries=0, http_client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body.encode()))))
        stream = client.chat.chat(messages=[{"role": "user", "content": "hi"}], model="m", stream=True)
        next(iter(stream))
        print("DONE")
    """)
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "DONE" in done.stdout
    assert "writer.chat" not in done.stdout, done.stdout


def test_stream_has_no_finalizer():
    from traceai_writer import _Stream

    assert "__del__" not in vars(_Stream)


def test_stream_response_is_still_the_sdk_response_class(spans):
    stream = make_client(stream_handler()).chat.chat(messages=MESSAGES, model="m", stream=True)
    assert isinstance(stream.response, httpx.Response)
    assert isinstance(stream, writerai.Stream)
    next(iter(stream))
    stream.close()


def test_async_stream_is_still_the_sdk_class(spans):
    async def go():
        stream = await make_async_client(stream_handler()).chat.chat(messages=MESSAGES, model="m", stream=True)
        assert isinstance(stream, writerai.AsyncStream)
        assert isinstance(stream.response, httpx.Response)
        await stream.close()

    asyncio.run(go())
