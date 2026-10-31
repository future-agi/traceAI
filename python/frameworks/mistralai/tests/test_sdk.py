"""Real Mistral SDK requests with synthetic in-process HTTP/SSE transports."""

import asyncio
import importlib
import json
from importlib.metadata import version

import pytest
from fi_instrumentation import TraceConfig, suppress_tracing
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_mistralai import MistralAIInstrumentor

NATIVE = int(version("mistralai").split(".")[0]) >= 3
SDK_ROOT = "mistralai.client" if NATIVE else "mistralai"
Mistral = importlib.import_module(SDK_ROOT).Mistral
httpx = importlib.import_module("httpx2" if NATIVE else "httpx")
MESSAGES = [{"role": "user", "content": "private-input-marker"}]
USAGE = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}


def chat_response():
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "mistral-test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "private-output-marker",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"test"}',
                            },
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": USAGE,
    }


def embedding_response():
    return {
        "id": "synthetic",
        "object": "list",
        "model": "mistral-embed",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2},
    }


def conversation_response():
    return {
        "object": "conversation.response",
        "conversation_id": "conv-test",
        "outputs": [
            {
                "type": "message.output",
                "role": "assistant",
                "content": "private-output-marker",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def completion_sse():
    for delta, finish in [
        ({"role": "assistant", "content": "private-output-marker"}, None),
        (
            {
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"test"}'},
                    }
                ],
            },
            "stop",
        ),
    ]:
        yield (
            "data: "
            + json.dumps(
                {
                    "id": "synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "mistral-test",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "usage": USAGE if finish else None,
                }
            )
            + "\n\n"
        ).encode()
    yield b"data: [DONE]\n\n"


class SSE(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False

    def __iter__(self):
        for chunk in completion_sse():
            yield chunk
            if self.fail:
                raise RuntimeError("private-stream-error-marker")

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    def close(self):
        self.closed = True

    async def aclose(self):
        self.closed = True


def conversation_sse(error=False):
    events = [
        {"type": "conversation.response.started", "conversation_id": "conv-test"},
        {
            "type": "message.output.delta",
            "id": "entry-test",
            "role": "assistant",
            "content": "private-output-marker",
        },
        {"type": "conversation.response.done", "usage": USAGE},
    ]
    if error:
        events[-1] = {
            "type": "conversation.response.error",
            "message": "private-error-marker",
            "code": 400,
        }
    return [
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    ]


class ConversationSSE(SSE):
    def __iter__(self):
        yield from conversation_sse(self.fail)


def transport_response(request):
    if request.url.path.endswith("embeddings"):
        return httpx.Response(200, json=embedding_response())
    if "conversations" in request.url.path:
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ConversationSSE(),
            )
        return httpx.Response(200, json=conversation_response())
    if json.loads(request.content).get("stream"):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE()
        )
    return httpx.Response(200, json=chat_response())


@pytest.fixture
def tracing(request):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    config = getattr(request, "param", TraceConfig())
    instrumentor.instrument(tracer_provider=provider, config=config)
    assert instrumentor.is_instrumented_by_opentelemetry
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


def params(resource):
    return {
        "messages": MESSAGES,
        **(
            {"model": "mistral-test"}
            if resource == "chat"
            else {"agent_id": "test-agent"}
        ),
    }


@pytest.mark.parametrize("resource", ["chat", "agents"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_completions(tracing, resource, streaming, asynchronous):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            client = Mistral(api_key="synthetic-test-key", async_client=transport)
            method = getattr(
                getattr(client, resource),
                "stream_async" if streaming else "complete_async",
            )
            result = await method(**params(resource))
            if streaming:
                chunks = [chunk async for chunk in result]
                if hasattr(result, "close"):
                    await result.close()
                return chunks[0].data.choices[0].delta.content
            return result.choices[0].message.content

    if asynchronous:
        text = asyncio.run(run())
    else:
        with httpx.Client(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            client = Mistral(api_key="synthetic-test-key", client=transport)
            result = getattr(
                getattr(client, resource), "stream" if streaming else "complete"
            )(**params(resource))
            if streaming:
                chunks = list(result)
                result.close()
                text = chunks[0].data.choices[0].delta.content
            else:
                text = result.choices[0].message.content
    assert text == "private-output-marker"
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 2
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 1
    assert "weather" in json.dumps(dict(spans[0].attributes))
    assert spans[0].status.status_code == StatusCode.OK


@pytest.mark.parametrize("asynchronous", [False, True])
def test_embeddings(tracing, asynchronous):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            return await Mistral(
                api_key="synthetic", async_client=transport
            ).embeddings.create_async(model="mistral-embed", inputs=["hi"])

    if asynchronous:
        result = asyncio.run(run())
    else:
        with httpx.Client(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            result = Mistral(api_key="synthetic", client=transport).embeddings.create(
                model="mistral-embed", inputs=["hi"]
            )
    assert result.data[0].embedding == [0.1, 0.2]
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.span.kind"] == "EMBEDDING"
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 2


@pytest.mark.skipif(
    not NATIVE, reason="Native conversation telemetry is supported on SDK >=3.6"
)
@pytest.mark.parametrize("method", ["start", "append", "restart"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_conversations(tracing, method, asynchronous, streaming):
    kwargs = {"inputs": "private-input-marker"}
    if method == "start":
        kwargs["model"] = "mistral-test"
    else:
        kwargs["conversation_id"] = "conv-test"
        if method == "restart":
            kwargs["from_entry_id"] = "entry-test"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            client = Mistral(api_key="synthetic", async_client=transport)
            result = await getattr(
                client.beta.conversations,
                method + ("_stream" if streaming else "") + "_async",
            )(**kwargs)
            if streaming:
                chunks = [chunk async for chunk in result]
                await result.close()
                assert chunks[-1].data.usage.prompt_tokens == 2
                return chunks[0].data.conversation_id
            return result.conversation_id

    if asynchronous:
        result = asyncio.run(run())
    else:
        with httpx.Client(
            transport=httpx.MockTransport(transport_response)
        ) as transport:
            client = Mistral(api_key="synthetic", client=transport)
            result = getattr(
                client.beta.conversations, method + ("_stream" if streaming else "")
            )(**kwargs)
            if streaming:
                chunks = list(result)
                result.close()
                assert chunks[-1].data.usage.prompt_tokens == 2
                result = chunks[0].data.conversation_id
            else:
                result = result.conversation_id
    assert result == "conv-test"
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.span.kind"] == "AGENT"
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 2


def test_existing_clients_suppression_reenable_and_parent():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with httpx.Client(transport=httpx.MockTransport(transport_response)) as transport:
        client = Mistral(api_key="synthetic", client=transport)
        original = type(client.chat).complete
        instrumentor = MistralAIInstrumentor()
        for _ in range(2):
            exporter.clear()
            instrumentor.instrument(tracer_provider=provider)
            instrumentor.instrument(tracer_provider=provider)
            if NATIVE:
                assert type(client.chat).complete is original
            with provider.get_tracer("parent").start_as_current_span(
                "parent"
            ) as parent:
                with suppress_tracing():
                    client.chat.complete(**params("chat"))
                client.chat.complete(**params("chat"))
            spans = exporter.get_finished_spans()
            assert len(spans) == 2
            assert spans[0].parent.span_id == parent.get_span_context().span_id
            instrumentor.uninstrument()
            exporter.clear()
            client.chat.complete(**params("chat"))
            assert not exporter.get_finished_spans()
    provider.shutdown()


@pytest.mark.parametrize(
    "tracing", [TraceConfig(hide_inputs=True, hide_outputs=True)], indirect=True
)
@pytest.mark.parametrize("error", [False, True])
def test_privacy_and_errors(tracing, error):
    def respond(request):
        if error:
            return httpx.Response(400, json={"message": "private-error-marker"})
        return transport_response(request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = Mistral(api_key="synthetic-key-marker", client=transport)
        if error:
            with pytest.raises(Exception, match="private-error-marker"):
                client.chat.complete(**params("chat"))
        else:
            import inspect

            options = (
                {"http_headers": {"x-private": "private-header-marker"}}
                if "http_headers" in inspect.signature(client.chat.complete).parameters
                else {}
            )
            client.chat.complete(**params("chat"), **options)
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    encoded = spans[0].to_json()
    for marker in (
        "private-input-marker",
        "private-output-marker",
        "private-error-marker",
        "private-header-marker",
        "synthetic-key-marker",
    ):
        assert marker not in encoded
    assert spans[0].status.status_code == (StatusCode.ERROR if error else StatusCode.OK)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_stream_early_close_and_errors(tracing, asynchronous, failure):
    stream = SSE(fail=failure)

    def respond(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            result = await Mistral(
                api_key="synthetic", async_client=transport
            ).chat.stream_async(**params("chat"))
            assert not tracing.get_finished_spans()
            async with result:
                if failure:
                    with pytest.raises(
                        RuntimeError, match="private-stream-error-marker"
                    ):
                        async for _ in result:
                            pass
                else:
                    await result.__anext__()

    if asynchronous:
        asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            result = Mistral(api_key="synthetic", client=transport).chat.stream(
                **params("chat")
            )
            assert not tracing.get_finished_spans()
            with result:
                if failure:
                    with pytest.raises(
                        RuntimeError, match="private-stream-error-marker"
                    ):
                        list(result)
                else:
                    next(result)
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == (
        StatusCode.ERROR if failure else StatusCode.OK
    )


@pytest.mark.skipif(
    not NATIVE, reason="Native conversation telemetry requires SDK >=3.6"
)
def test_native_conversation_error_event(tracing):
    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ConversationSSE(fail=True),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = Mistral(api_key="synthetic", client=transport)
        with client.beta.conversations.start_stream(
            model="mistral-test", inputs="hello"
        ) as result:
            chunks = list(result)
            assert chunks[-1].data.type == "conversation.response.error"
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_concurrent_async_calls(tracing):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport_response)) as transport:
            client = Mistral(api_key="synthetic", async_client=transport)
            results = await asyncio.gather(*(client.chat.complete_async(**params("chat")) for _ in range(24)))
            assert all(result.choices[0].message.content == "private-output-marker" for result in results)
    asyncio.run(run())
    spans = tracing.get_finished_spans()
    assert len(spans) == 24
    assert len({span.context.span_id for span in spans}) == 24


@pytest.mark.skipif(not NATIVE, reason="Native provider binding requires SDK >=3.6")
def test_restore_existing_native_provider():
    from mistralai.extra.observability import configure_telemetry
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    other = TracerProvider()
    other_exporter = InMemorySpanExporter()
    other.add_span_processor(SimpleSpanProcessor(other_exporter))
    with httpx.Client(transport=httpx.MockTransport(transport_response)) as transport:
        client = Mistral(api_key="synthetic", client=transport)
        configure_telemetry(client, provider=other, redaction=False)
        instrumentor = MistralAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        try:
            client.chat.complete(**params("chat"))
            assert len(exporter.get_finished_spans()) == 1
            assert not other_exporter.get_finished_spans()
        finally:
            instrumentor.uninstrument()
        client.chat.complete(**params("chat"))
        assert len(other_exporter.get_finished_spans()) == 1
        assert len(exporter.get_finished_spans()) == 1
    provider.shutdown()
    other.shutdown()


def test_sampler_failure_does_not_change_response(caplog):
    from opentelemetry.sdk.trace.sampling import Sampler
    class BrokenSampler(Sampler):
        def should_sample(self, *args, **kwargs):
            raise RuntimeError("synthetic telemetry failure")
        def get_description(self):
            return "broken-test-sampler"
    provider = TracerProvider(sampler=BrokenSampler())
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with httpx.Client(transport=httpx.MockTransport(transport_response)) as transport:
            result = Mistral(api_key="synthetic", client=transport).chat.complete(**params("chat"))
        assert result.choices[0].message.content == "private-output-marker"
        assert caplog.records
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
