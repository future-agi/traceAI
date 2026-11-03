"""Boundary cases for the Mistral privacy and native stream repairs."""

import asyncio
import json

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode
from traceai_mistralai import MistralAIInstrumentor
from traceai_mistralai._privacy import _NativeTraceConfig

from test_review_regressions import (
    CHAT,
    EMBED,
    NATIVE,
    EventStream,
    Mistral,
    httpx,
    tracing,
)


@pytest.mark.parametrize("key", ["http.url", "url.full"])
@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://user:password@proxy.invalid:8443/v1?q=ok#stream",
            "https://proxy.invalid:8443/v1",
        ),
        ("https://user%40name:p%40ss@proxy.invalid/v1", "https://proxy.invalid/v1"),
        ("http://user:password@[::1]:8080/v1", "http://[::1]:8080/v1"),
        ("https://user@proxy.invalid/v1", "https://proxy.invalid/v1"),
        ("https://proxy.invalid/v1", "https://proxy.invalid/v1"),
        ("https://user:password@[invalid/v1", None),
    ],
)
def test_url_mask_removes_userinfo_query_and_fragment(key, lazy, url, expected):
    config = _NativeTraceConfig(TraceConfig())
    value = (lambda: url) if lazy else url
    assert config.mask(key, value) == expected


@pytest.mark.parametrize("key", ["http.url", "url.full"])
def test_url_mask_drops_query_credentials_and_fragment(key):
    url = "https://api.mistral.ai/v1/chat/completions?api_key=synthetic-query-credential-458#private-fragment"
    assert _NativeTraceConfig(TraceConfig(hide_inputs=True, hide_outputs=True)).mask(
        key, url
    ) == "https://api.mistral.ai/v1/chat/completions"


@pytest.mark.skipif(not NATIVE, reason="Native SDK HTTP attributes")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide_content", [False, True])
def test_proxy_credentials_not_exported_but_request_unchanged(
    asynchronous, hide_content
):
    observed = []

    def respond(request):
        observed.append(str(request.url))
        return httpx.Response(200, json=CHAT)

    with tracing(hide_inputs=hide_content, hide_outputs=hide_content) as (
        exporter,
        starts,
    ):
        options = dict(
            api_key="synthetic-key",
            server_url="https://synthetic-user:synthetic-password@proxy.invalid:8443",
        )
        params = dict(
            model="mistral-test", messages=[{"role": "user", "content": "hello"}]
        )
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    await Mistral(
                        async_client=transport, **options
                    ).chat.complete_async(**params)

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                Mistral(client=transport, **options).chat.complete(**params)
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == len(observed) == 1
        assert "synthetic-user:synthetic-password@" in observed[0]
        assert "synthetic-password" not in spans[0].to_json()
        assert "synthetic-user" not in spans[0].to_json()
        assert (
            spans[0].attributes["http.url"]
            == "https://proxy.invalid:8443/v1/chat/completions"
        )


@pytest.mark.skipif(not NATIVE, reason="Native SDK HTTP attributes")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide_content", [False, True])
def test_query_credentials_not_exported_but_request_unchanged(
    asynchronous, hide_content
):
    marker = "synthetic-query-credential-458"
    observed = []

    def respond(request):
        observed.append(str(request.url))
        return httpx.Response(200, json=CHAT)

    with tracing(hide_inputs=hide_content, hide_outputs=hide_content) as (
        exporter,
        starts,
    ):
        params = dict(
            model="mistral-test", messages=[{"role": "user", "content": "hello"}]
        )
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    params={"api_key": marker}, transport=httpx.MockTransport(respond)
                ) as transport:
                    await Mistral(
                        api_key="synthetic-key", async_client=transport
                    ).chat.complete_async(**params)

            asyncio.run(exercise())
        else:
            with httpx.Client(
                params={"api_key": marker}, transport=httpx.MockTransport(respond)
            ) as transport:
                Mistral(
                    api_key="synthetic-key", client=transport
                ).chat.complete(**params)
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == len(observed) == 1
        assert marker in observed[0]
        assert marker not in spans[0].to_json()
        assert (
            spans[0].attributes["http.url"]
            == "https://api.mistral.ai/v1/chat/completions"
        )


@pytest.mark.skipif(NATIVE, reason="Legacy embedding extraction")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize(
    "flag",
    [
        None,
        "hide_inputs",
        "hide_input_messages",
        "hide_input_text",
        "hide_outputs",
        "hide_embedding_vectors",
    ],
)
def test_embedding_privacy_preserves_request_and_allowed_text(
    asynchronous, single, flag
):
    marker = "synthetic-private-embedding-input"
    inputs = marker if single else [marker, "second-input"]
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=EMBED)

    with tracing(**({flag: True} if flag else {})) as (exporter, starts):
        params = dict(model="mistral-embed", inputs=inputs)
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    await Mistral(
                        api_key="synthetic-key", async_client=transport
                    ).embeddings.create_async(**params)

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                Mistral(api_key="synthetic-key", client=transport).embeddings.create(
                    **params
                )
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == len(observed) == 1
        assert observed[0]["input"] == inputs
        attrs = spans[0].attributes
        assert marker not in attrs.get("gen_ai.request.parameters", "")
        assert "input.value" not in attrs
        assert attrs["gen_ai.span.kind"] == "EMBEDDING"
        hidden = flag in ("hide_inputs", "hide_input_messages", "hide_input_text")
        assert (marker in spans[0].to_json()) is not hidden
        if not hidden:
            assert attrs["embedding.embeddings.0.embedding.text"] == marker


@pytest.mark.skipif(NATIVE, reason="Legacy embedding cancellation")
@pytest.mark.parametrize("hide_error_details", [False, True])
def test_cancel_preserves_exception_identity_and_ends_once(hide_error_details):
    error = asyncio.CancelledError("synthetic-cancellation-detail")
    with tracing(hide_error_details=hide_error_details) as (exporter, starts):

        async def exercise():
            async def respond(request):
                raise error

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as transport:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await Mistral(
                        api_key="synthetic-key", async_client=transport
                    ).embeddings.create_async(model="mistral-embed", inputs=["hello"])
                assert caught.value is error

        asyncio.run(exercise())
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 1
        assert not starts.spans[0].is_recording()
        assert spans[0].status.status_code == StatusCode.ERROR
        if hide_error_details:
            assert "synthetic-cancellation-detail" not in spans[0].to_json()


@pytest.mark.skipif(not NATIVE, reason="Native conversation delta reconstruction")
@pytest.mark.parametrize("asynchronous", [False, True])
def test_interleaved_conversation_calls_are_independent(asynchronous):
    events = [{"type": "conversation.response.started", "conversation_id": "conv-test"}]
    for index, fragment in [
        (0, '{"city":'),
        (1, '{"date":'),
        (0, '"Paris"}'),
        (1, '"Monday"}'),
    ]:
        events.append(
            {
                "type": "function.call.delta",
                "id": f"entry-{index}",
                "tool_call_id": f"call-{index}",
                "output_index": index,
                "name": ["weather", "calendar"][index],
                "arguments": fragment,
            }
        )
    events.append(
        {
            "type": "conversation.response.done",
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
    )

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventStream(events, named=True),
        )

    with tracing() as (exporter, starts):
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic-key", async_client=transport)
                    async with await client.beta.conversations.start_stream_async(
                        model="mistral-test", inputs="hello"
                    ) as stream:
                        assert len([event async for event in stream]) == 6

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic-key", client=transport)
                with client.beta.conversations.start_stream(
                    model="mistral-test", inputs="hello"
                ) as stream:
                    assert len(list(stream)) == 6
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 1
        parts = json.loads(spans[0].attributes["gen_ai.output.messages"])[0]["parts"]
        assert parts == [
            {
                "type": "tool_call",
                "id": "call-0",
                "name": "weather",
                "arguments": '{"city":"Paris"}',
            },
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "calendar",
                "arguments": '{"date":"Monday"}',
            },
        ]


class RawEvents(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    def __iter__(self):
        yield self.payload

    async def __aiter__(self):
        yield self.payload


@pytest.mark.skipif(not NATIVE, reason="Native SDK decoder lifetime")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide_error_details", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        b'data: {"error":{"message":"synthetic-private-error"}}',
        b'data: {"error":{"message":"synthetic-private-error"}}\n\n',
        b"data: \xff\n\n",
    ],
)
def test_decode_error_ends_span_even_without_context_manager(
    asynchronous, hide_error_details, payload
):
    responses = []

    def respond(request):
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=RawEvents(payload),
        )
        responses.append(response)
        return response

    with tracing(hide_error_details=hide_error_details) as (exporter, starts):
        params = dict(
            model="mistral-test", messages=[{"role": "user", "content": "hello"}]
        )
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    stream = await Mistral(
                        api_key="synthetic-key", async_client=transport
                    ).chat.stream_async(**params)
                    with pytest.raises(Exception) as caught:
                        [event async for event in stream]
                    assert "Validation" in type(caught.value).__name__ or isinstance(
                        caught.value, UnicodeDecodeError
                    )
                    assert stream.response.is_closed
                    await stream.close()

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                stream = Mistral(api_key="synthetic-key", client=transport).chat.stream(
                    **params
                )
                with pytest.raises(Exception) as caught:
                    list(stream)
                assert "Validation" in type(caught.value).__name__ or isinstance(
                    caught.value, UnicodeDecodeError
                )
                assert stream.response.is_closed
                stream.close()
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 1
        assert not starts.spans[0].is_recording()
        assert spans[0].status.status_code == StatusCode.ERROR
        if hide_error_details:
            assert "synthetic-private-error" not in spans[0].to_json()
        else:
            assert len(spans[0].events) == 1


@pytest.mark.skipif(not NATIVE, reason="Native stream wrapper restoration")
def test_sdk_iteration_methods_restored():
    from mistralai.client.utils.eventstreaming import (
        EventStream as SDKStream,
        EventStreamAsync,
    )

    original_sync, original_async = SDKStream.__next__, EventStreamAsync.__anext__
    for _ in range(2):
        with tracing():
            assert SDKStream.__next__ is not original_sync
            assert EventStreamAsync.__anext__ is not original_async
        assert SDKStream.__next__ is original_sync
        assert EventStreamAsync.__anext__ is original_async
        assert not MistralAIInstrumentor().is_instrumented_by_opentelemetry
