"""Offline stream contracts against real Hub SDK parsers and OTLP serialization."""

import asyncio
import json
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from huggingface_hub import AsyncInferenceClient, InferenceClient
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.trace import StatusCode
from traceai_huggingface import HuggingFaceInstrumentor
from traceai_huggingface._wrappers import (
    _AsyncChatCompletionWrapper,
    _AsyncTextGenerationWrapper,
    _ChatCompletionWrapper,
    _TextGenerationWrapper,
)


def text_chunks():
    return [
        {
            "token": {
                "id": 1,
                "text": "private hello",
                "logprob": -0.1,
                "special": False,
            }
        },
        {"token": {"id": 2, "text": " world", "logprob": -0.1, "special": False}},
        {
            "token": {"id": 3, "text": "</s>", "logprob": -0.1, "special": True},
            "generated_text": "private hello world",
            "details": {"generated_tokens": 2, "finish_reason": "eos_token", "seed": 0},
        },
    ]


def chat_chunks():
    def chunk(choices, **extra):
        return {
            "id": "offline-id",
            "model": "offline-model",
            "created": 0,
            "system_fingerprint": "offline",
            "choices": choices,
            **extra,
        }

    def choice(index, content=None, tools=None, finish=None):
        delta = {"role": "assistant", "content": content}
        if tools is not None:
            delta["tool_calls"] = tools
        return {"index": index, "delta": delta, "finish_reason": finish}

    def tool(index, name, arguments, identifier=None):
        return {
            "index": index,
            "id": identifier,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }

    return [
        chunk(
            [
                choice(1, "other answer"),
                choice(
                    0,
                    "private hello",
                    [
                        tool(1, "second", '{"b":', "call-b"),
                        tool(0, "first", '{"secret":', "call-a"),
                    ],
                ),
            ]
        ),
        chunk(
            [
                choice(
                    0,
                    " world",
                    [
                        tool(0, None, '"private argument"}'),
                        tool(1, None, "2}"),
                    ],
                    "tool_calls",
                ),
                choice(1, finish="stop"),
            ]
        ),
        chunk(
            [], usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}
        ),
    ]


def install_transport(monkeypatch, chunks, asynchronous, seen, error=None):
    # Hub 0.x uses byte lines through post(); 2.x uses str through _inner_post().
    modern = int(version("huggingface-hub").split(".")[0]) >= 2
    lines = ["data: " + json.dumps(chunk) + "\n\n" for chunk in chunks]
    if not modern:
        lines = [line.encode() for line in lines]

    def sync_post(self, *args, **kwargs):
        assert kwargs["stream"]

        def stream():
            for line in lines:
                seen.append(trace.get_current_span().get_span_context().span_id)
                yield line
            if error is not None:
                raise error

        return stream()

    async def async_post(self, *args, **kwargs):
        assert kwargs["stream"]

        async def stream():
            for line in lines:
                seen.append(trace.get_current_span().get_span_context().span_id)
                yield line
            if error is not None:
                raise error

        return stream()

    client_class = AsyncInferenceClient if asynchronous else InferenceClient
    transport = "_inner_post" if hasattr(client_class, "_inner_post") else "post"
    monkeypatch.setattr(
        client_class, transport, async_post if asynchronous else sync_post
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("hide_outputs", [False, True])
def test_sdk_stream_lifetime_metadata_and_otlp(
    monkeypatch, telemetry, asynchronous, chat, hide_outputs
):
    if chat and not hasattr(InferenceClient, "chat_completion"):
        pytest.skip("chat_completion was not present in huggingface-hub 0.20.0")
    provider, exporter = telemetry
    seen = []
    chunks = chat_chunks() if chat else text_chunks()
    install_transport(monkeypatch, chunks, asynchronous, seen)
    HuggingFaceInstrumentor().instrument(
        tracer_provider=provider, config=TraceConfig(hide_outputs=hide_outputs)
    )
    params = (
        {"messages": [{"role": "user", "content": "hello"}]}
        if chat
        else {"prompt": "hello", "details": True}
    )
    method = "chat_completion" if chat else "text_generation"
    tracer = provider.get_tracer("test")

    async def consume_async(parent):
        client = AsyncInferenceClient(
            model="http://offline.invalid", token="synthetic-not-real"
        )
        stream = await getattr(client, method)(**params, stream=True)
        assert exporter.get_finished_spans() == ()
        assert seen == []
        count = 0
        async for _ in stream:
            count += 1
            assert trace.get_current_span() is parent
            assert exporter.get_finished_spans() == ()
        assert count == len(chunks)
        await stream.aclose()  # Already finished: must not export twice.
        if hasattr(client, "close"):
            await client.close()

    with tracer.start_as_current_span("parent") as parent:
        if asynchronous:
            asyncio.run(consume_async(parent))
        else:
            client = InferenceClient(
                model="http://offline.invalid", token="synthetic-not-real"
            )
            stream = getattr(client, method)(**params, stream=True)
            assert exporter.get_finished_spans() == ()
            assert seen == []
            count = 0
            for _ in stream:
                count += 1
                assert trace.get_current_span() is parent
                assert exporter.get_finished_spans() == ()
            assert count == len(chunks)
            stream.close()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent.span_id == parent.get_span_context().span_id
    assert seen == [span.context.span_id] * len(chunks)
    assert span.status.status_code == StatusCode.OK
    attrs = span.attributes
    assert attrs["huggingface.stream.status"] == "completed"
    assert attrs["gen_ai.usage.output_tokens"] == (4 if chat else 2)
    assert attrs["gen_ai.response.finish_reasons"] == (
        ("tool_calls", "stop") if chat else ("eos_token",)
    )
    if chat:
        assert attrs["gen_ai.usage.input_tokens"] == 7
        assert attrs["gen_ai.usage.total_tokens"] == 11
        assert attrs["gen_ai.response.model"] == "offline-model"
        assert attrs["gen_ai.response.id"] == "offline-id"
    if hide_outputs:
        assert attrs["output.value"] == "__REDACTED__"
        assert not any(key.startswith("gen_ai.output.messages") for key in attrs)
    else:
        assert (
            attrs["gen_ai.output.messages.0.message.content"] == "private hello world"
        )
        result = json.loads(attrs["output.value"])
        if chat:
            tools = result["choices"][0]["message"]["tool_calls"]
            assert len(tools) == 2
            assert tools[0] == {
                "id": "call-a",
                "type": "function",
                "function": {
                    "name": "first",
                    "arguments": '{"secret":"private argument"}',
                },
            }
            assert tools[1]["function"]["arguments"] == '{"b":2}'
            assert result["choices"][1]["message"]["content"] == "other answer"
            assert (
                attrs[
                    "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
                ]
                == '{"secret":"private argument"}'
            )
        else:
            assert result["generated_text"] == "private hello world"
    # Encode and decode the actual OTLP protobuf, not only a Python span mock.
    wire = encode_spans([span]).SerializeToString()
    decoded = ExportTraceServiceRequest.FromString(wire)
    wire_span = decoded.resource_spans[0].scope_spans[0].spans[0]
    assert wire_span.name == "huggingface." + method
    assert wire_span.end_time_unix_nano >= wire_span.start_time_unix_nano
    if hide_outputs:
        assert b"private hello" not in wire
        assert b"private argument" not in wire
    else:
        assert b"private hello world" in wire


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("chat", [False, True])
def test_sdk_stream_failure_preserves_partial_output(
    monkeypatch, telemetry, asynchronous, chat
):
    if chat and not hasattr(InferenceClient, "chat_completion"):
        pytest.skip("chat_completion was not present in huggingface-hub 0.20.0")
    provider, exporter = telemetry
    error = RuntimeError("synthetic stream failure")
    chunks = (chat_chunks() if chat else text_chunks())[:1]
    install_transport(monkeypatch, chunks, asynchronous, [], error)
    HuggingFaceInstrumentor().instrument(tracer_provider=provider)
    params = (
        {"messages": [{"role": "user", "content": "hello"}]}
        if chat
        else {"prompt": "hello", "details": True}
    )
    method = "chat_completion" if chat else "text_generation"

    async def run():
        client = AsyncInferenceClient(
            model="http://offline.invalid", token="synthetic-not-real"
        )
        stream = await getattr(client, method)(**params, stream=True)
        assert exporter.get_finished_spans() == ()
        with pytest.raises(RuntimeError) as raised:
            async for _ in stream:
                pass
        assert raised.value is error
        await stream.aclose()
        if hasattr(client, "close"):
            await client.close()

    if asynchronous:
        asyncio.run(run())
    else:
        client = InferenceClient(
            model="http://offline.invalid", token="synthetic-not-real"
        )
        stream = getattr(client, method)(**params, stream=True)
        with pytest.raises(RuntimeError) as raised:
            list(stream)
        assert raised.value is error
        stream.close()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert "private hello" in spans[0].attributes["output.value"]
    assert spans[0].events[0].attributes["exception.type"] == "RuntimeError"


def wrapped_stream(telemetry, source, asynchronous, chat=False, config=None):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("test"), config=config or TraceConfig())
    wrapper_class = (
        (_AsyncChatCompletionWrapper if chat else _AsyncTextGenerationWrapper)
        if asynchronous
        else (_ChatCompletionWrapper if chat else _TextGenerationWrapper)
    )
    wrapper = wrapper_class(tracer=tracer)

    async def call(*args, **kwargs):
        return source

    if asynchronous:
        return wrapper(call, SimpleNamespace(model="offline"), (), {"stream": True})
    return wrapper(
        lambda **kwargs: source, SimpleNamespace(model="offline"), (), {"stream": True}
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("chat", [False, True])
def test_close_is_forwarded_and_finishes_once(telemetry, asynchronous, started, chat):
    provider, exporter = telemetry

    class Source:
        closed = 0

        def __iter__(self):
            return self

        def __next__(self):
            return chat_chunks()[0] if chat else "private partial"

        def __aiter__(self):
            return self

        async def __anext__(self):
            return next(self)

        def close(self):
            self.closed += 1

        async def aclose(self):
            self.close()

    source = Source()

    async def run():
        stream = await wrapped_stream(telemetry, source, True, chat)
        assert exporter.get_finished_spans() == ()
        if started:
            await stream.__anext__()
        await stream.aclose()
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    if asynchronous:
        asyncio.run(run())
    else:
        stream = wrapped_stream(telemetry, source, False, chat)
        if started:
            next(stream)
        stream.close()
        stream.close()
        with pytest.raises(StopIteration):
            next(stream)
    assert source.closed == 1
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.UNSET
    assert spans[0].attributes["huggingface.stream.status"] == "closed"
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("during_call", [False, True])
@pytest.mark.parametrize("chat", [False, True])
def test_task_cancellation_ends_span_and_preserves_error(telemetry, during_call, chat):
    provider, exporter = telemetry

    async def run():
        entered = asyncio.Event()

        async def source():
            entered.set()
            await asyncio.Future()
            yield "unreachable"

        async def call(**kwargs):
            entered.set()
            await asyncio.Future()

        async def task_body():
            if during_call:
                tracer = FITracer(provider.get_tracer("test"), config=TraceConfig())
                wrapper = (
                    _AsyncChatCompletionWrapper if chat else _AsyncTextGenerationWrapper
                )(tracer=tracer)
                await wrapper(
                    call, SimpleNamespace(model="offline"), (), {"stream": True}
                )
            else:
                stream = await wrapped_stream(telemetry, source(), True, chat)
                await stream.__anext__()

        task = asyncio.create_task(task_body())
        await entered.wait()
        assert exporter.get_finished_spans() == ()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].attributes["exception.type"].endswith("CancelledError")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_suppression_returns_original_stream(telemetry, asynchronous):
    source = object()
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        response = wrapped_stream(telemetry, source, asynchronous)
        if asynchronous:
            response = asyncio.run(response)
        assert response is source
    finally:
        context.detach(token)
    assert telemetry[1].get_finished_spans() == ()


def test_nonstream_async_response_still_finishes_immediately(telemetry):
    provider, exporter = telemetry
    response = {"generated_text": "hello", "details": {"generated_tokens": 1}}

    async def call(**kwargs):
        return response

    wrapper = _AsyncTextGenerationWrapper(
        tracer=FITracer(provider.get_tracer("test"), config=TraceConfig())
    )
    assert (
        asyncio.run(wrapper(call, SimpleNamespace(model="offline"), (), {})) is response
    )
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.OK
