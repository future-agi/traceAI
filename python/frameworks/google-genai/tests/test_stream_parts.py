"""Keep Gemini's ordered, unindexed parts distinct in streamed traces."""

import asyncio
import json

import httpx
import pytest
from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from google import genai
from google.genai import _api_client, types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_google_genai import GoogleGenAIInstrumentor
from traceai_google_genai._stream import _Stream
from traceai_google_genai._with_span import _WithSpan


def _response(parts, index=0, **kwargs):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                index=index, content=types.Content(role="model", parts=parts)
            )
        ],
        **kwargs,
    )


def _call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def _messages(span):
    return json.loads(span.attributes[SpanAttributes.OUTPUT_VALUE])["candidates"]


def _assert_calls(span, calls):
    prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_TOOL_CALLS}"
    names = {
        key: value
        for key, value in span.attributes.items()
        if key.startswith(prefix)
        and key.endswith(ToolCallAttributes.TOOL_CALL_FUNCTION_NAME)
    }
    assert names == {
        f"{prefix}.{i}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}": call.name
        for i, call in enumerate(calls)
    }
    for i, call in enumerate(calls):
        assert (
            json.loads(
                span.attributes[
                    f"{prefix}.{i}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}"
                ]
            )
            == call.args
        )


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


def _consume(chunks, asynchronous, with_span):
    if not asynchronous:
        return list(_Stream(iter(chunks), with_span))

    async def consume():
        async def source():
            for chunk in chunks:
                yield chunk

        return [chunk async for chunk in _Stream(source(), with_span)]

    return asyncio.run(consume())


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "groups",
    [
        [[_call("weather", city="Paris"), _call("time", zone="UTC")]],
        [[_call("weather", city="Paris")], [_call("weather", city="London")]],
        [
            [types.Part(text="Checking "), _call("weather", city="Paris")],
            [_call("time", zone="UTC"), types.Part(text="now.")],
        ],
        pytest.param(
            [
                [
                    {
                        "function_call": {"name": "weather", "args": {"city": "Paris"}},
                        "thought_signature": b"first-signature",
                    },
                    {
                        "function_call": {"name": "time", "args": {"zone": "UTC"}},
                        "thought_signature": b"second-signature",
                    },
                ]
            ],
            marks=pytest.mark.skipif(
                "thought_signature" not in types.Part.model_fields,
                reason="This SDK predates thought signatures",
            ),
        ),
        [
            [
                types.Part(
                    file_data=types.FileData(
                        file_uri="gs://example/first", mime_type="image/png"
                    )
                )
            ],
            [
                types.Part(
                    file_data=types.FileData(
                        file_uri="gs://example/second", mime_type="image/png"
                    )
                )
            ],
        ],
        [[types.Part(text="Hello ")], [types.Part(text="world")]],
        [[_call("weather", city="Paris")]],
    ],
    ids=[
        "parallel",
        "repeated-name",
        "text-and-tools",
        "signatures",
        "media",
        "text",
        "single-part",
    ],
)
def test_stream_preserves_parts(tracing, asynchronous, groups):
    provider, exporter = tracing
    groups = [[types.Part.model_validate(part) for part in parts] for parts in groups]
    chunks = [_response(parts) for parts in groups]
    chunks.append(
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(index=0, finish_reason=types.FinishReason.STOP)
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=3, candidates_token_count=4, total_token_count=7
            ),
            model_version="gemini-test",
        )
    )
    snapshots = [chunk.model_dump(exclude_unset=True) for chunk in chunks]
    span = provider.get_tracer(__name__).start_span("stream")
    received = _consume(chunks, asynchronous, _WithSpan(span))

    assert len(received) == len(chunks)
    assert all(actual is expected for actual, expected in zip(received, chunks))
    assert [chunk.model_dump(exclude_unset=True) for chunk in chunks] == snapshots
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    result = finished[0]
    expected_parts = [part for group in groups for part in group]
    # Preserve the existing trace serialization, including bytes signatures.
    expected = json.loads(
        safe_json_dumps(
            [part.model_dump(exclude_unset=True) for part in expected_parts]
        )
    )
    actual = _messages(result)[0]
    assert actual["content"]["parts"] == expected
    assert actual["finish_reason"] == "STOP"
    assert result.status.status_code == StatusCode.OK
    assert result.attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 3
    assert result.attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 4
    assert result.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 7
    _assert_calls(result, [p.function_call for p in expected_parts if p.function_call])
    texts = [p.text for p in expected_parts if p.text]
    if texts:
        assert result.attributes[
            f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
        ] == "".join(texts)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_empty_stream_finishes_without_output(tracing, asynchronous):
    provider, exporter = tracing
    assert (
        _consume(
            [],
            asynchronous,
            _WithSpan(provider.get_tracer(__name__).start_span("stream")),
        )
        == []
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert SpanAttributes.OUTPUT_VALUE not in spans[0].attributes
    assert spans[0].status.status_code == StatusCode.OK


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_stream_keeps_candidate_indexes_separate(tracing, asynchronous):
    provider, exporter = tracing
    chunks = [
        types.GenerateContentResponse(
            candidates=[
                _response([_call("first", x=1)], index=1).candidates[0],
                _response([_call("zeroth", x=0)], index=0).candidates[0],
            ]
        ),
        _response([_call("second", x=2)], index=1),
    ]
    _consume(
        chunks,
        asynchronous,
        _WithSpan(provider.get_tracer(__name__).start_span("stream")),
    )
    messages = _messages(exporter.get_finished_spans()[0])
    assert [
        [part["function_call"]["name"] for part in candidate["content"]["parts"]]
        for candidate in messages
    ] == [["zeroth"], ["first", "second"]]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_real_sdk_stream_exports_all_tool_calls(tracing, asynchronous, monkeypatch):
    provider, exporter = tracing
    chunks = [
        _response([_call("weather", city="Paris"), _call("time", zone="UTC")]),
        _response([types.Part(text="Done.")]),
    ]
    requests = []

    def respond(request):
        requests.append(request)
        assert ":streamGenerateContent" in request.url.path
        assert request.url.params["alt"] == "sse"
        data = "".join(
            f"data: {chunk.model_dump_json(exclude_unset=True, by_alias=True)}\n\n"
            for chunk in chunks
        )
        return httpx.Response(
            200, text=data, headers={"content-type": "text/event-stream"}
        )

    transport = httpx.MockTransport(respond)
    sync_http = httpx.Client(transport=transport)
    async_http = httpx.AsyncClient(transport=transport)
    # Inject at the HTTP boundary so SDK versions before HttpOptions.client_args
    # also exercise real request building, SSE parsing and response conversion.
    monkeypatch.setattr(_api_client, "SyncHttpxClient", lambda **kwargs: sync_http)
    monkeypatch.setattr(_api_client, "AsyncHttpxClient", lambda **kwargs: async_http)
    client = genai.Client(api_key="offline-test-key")
    instrumentor = GoogleGenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        if asynchronous:

            async def consume():
                try:
                    stream = await client.aio.models.generate_content_stream(
                        model="gemini-test", contents="Run both tools."
                    )
                    return [chunk async for chunk in stream]
                finally:
                    await async_http.aclose()

            received = asyncio.run(consume())
        else:
            received = list(
                client.models.generate_content_stream(
                    model="gemini-test", contents="Run both tools."
                )
            )
        assert len(requests) == 1
        assert received[0].function_calls == [
            part.function_call for part in chunks[0].candidates[0].content.parts
        ]
        assert received[1].text == "Done."
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        _assert_calls(spans[0], received[0].function_calls)
        assert len(_messages(spans[0])[0]["content"]["parts"]) == 3
    finally:
        instrumentor.uninstrument()
        sync_http.close()
        if not async_http.is_closed:
            asyncio.run(async_http.aclose())
