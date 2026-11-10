"""Stream text deltas coalesce without erasing call or media boundaries."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from traceai_google_genai._stream import _Stream
from traceai_google_genai._with_span import _WithSpan


class _Chunk:
    def __init__(self, parts, index=0):
        self.values = {
            "candidates": [
                {"index": index, "content": {"role": "model", "parts": parts}}
            ]
        }

    def model_dump(self, **kwargs):
        return self.values


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "groups, expected",
    [
        (
            [[{"text": "hello"}], [{"text": " world"}]],
            [{"text": "hello world"}],
        ),
        (
            [[{"text": "hello"}, {"text": " world"}]],
            [{"text": "hello world"}],
        ),
        (
            [
                [{"text": "hello"}],
                [{"function_call": {"name": "weather", "args": {"city": "Paris"}}}],
                [{"text": " world"}],
            ],
            [
                {"text": "hello"},
                {"function_call": {"name": "weather", "args": {"city": "Paris"}}},
                {"text": " world"},
            ],
        ),
        (
            [[
                {"function_call": {"name": "weather", "args": {"city": "Paris"}}},
                {"function_call": {"name": "time", "args": {"zone": "UTC"}}},
            ]],
            [
                {"function_call": {"name": "weather", "args": {"city": "Paris"}}},
                {"function_call": {"name": "time", "args": {"zone": "UTC"}}},
            ],
        ),
        (
            [
                [{"function_call": {"name": "weather", "args": {"city": "Paris"}}}],
                [{"function_call": {"name": "weather", "args": {"city": "Paris"}}}],
            ],
            [
                {"function_call": {"name": "weather", "args": {"city": "Paris"}}},
                {"function_call": {"name": "weather", "args": {"city": "Paris"}}},
            ],
        ),
        (
            [
                [{"function_call": {"id": "call-a", "name": "weather", "args": {"city": "Paris"}}}],
                [{"function_call": {"id": "call-b", "name": "weather", "args": {"city": "London"}}}],
                [{"function_call": {"id": "call-a", "args": {"units": "C"}}}],
            ],
            [
                {"function_call": {"id": "call-a", "name": "weather", "args": {"city": "Paris", "units": "C"}}},
                {"function_call": {"id": "call-b", "name": "weather", "args": {"city": "London"}}},
            ],
        ),
        (
            [
                [{"text": "hello"}],
                [{"file_data": {"file_uri": "gs://example/image", "mime_type": "image/png"}}],
                [{"text": " world"}],
            ],
            [
                {"text": "hello"},
                {"file_data": {"file_uri": "gs://example/image", "mime_type": "image/png"}},
                {"text": " world"},
            ],
        ),
        (
            [
                [{"text": "thinking", "thought": True}],
                [{"text": "hello"}],
                [{"text": " world"}],
            ],
            [{"text": "thinking", "thought": True}, {"text": "hello world"}],
        ),
        (
            [
                [{"text": "hello"}],
                [{"function_call": {"id": "call-a", "name": "weather", "args": {"city": "Paris"}}}],
                [{"text": " world"}],
                [{"function_call": {"id": "call-a", "args": {"units": "C"}}}],
                [{"text": "!"}],
            ],
            [
                {"text": "hello"},
                {"function_call": {"id": "call-a", "name": "weather", "args": {"city": "Paris", "units": "C"}}},
                {"text": " world"},
                {"text": "!"},
            ],
        ),
    ],
    ids=[
        "adjacent-text-chunks",
        "adjacent-text-parts",
        "text-call-text",
        "parallel-calls",
        "identical-indexless-calls",
        "explicit-call-id-deltas",
        "text-media-text",
        "thought-boundary",
        "call-delta-breaks-text-run",
    ],
)
def test_stream_accumulation(asynchronous, groups, expected):
    chunks = [_Chunk(parts) for parts in groups]
    snapshots = deepcopy([chunk.values for chunk in chunks])
    span = Mock(spec=trace_api.Span)
    span.is_recording.return_value = True
    with_span = Mock(wraps=_WithSpan(span))

    if asynchronous:
        async def consume():
            async def source():
                for chunk in chunks:
                    yield chunk

            stream = _Stream(source(), with_span)
            return [chunk async for chunk in stream]

        received = asyncio.run(consume())
    else:
        stream = _Stream(iter(chunks), with_span)
        received = list(stream)

    assert all(actual is original for actual, original in zip(received, chunks))
    assert len(received) == len(chunks)
    assert [chunk.values for chunk in chunks] == snapshots
    with_span.finish_tracing.assert_called_once()
    span.end.assert_called_once_with()
    kwargs = with_span.finish_tracing.call_args.kwargs
    assert kwargs["status"].status_code == trace_api.StatusCode.OK
    output = json.loads(kwargs["attributes"][SpanAttributes.OUTPUT_VALUE])
    assert output["candidates"][0]["content"]["parts"] == expected
