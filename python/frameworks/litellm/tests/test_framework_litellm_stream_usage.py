"""Offline regression coverage for T3-LITELLM-006."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


@pytest.mark.parametrize("async_stream", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("chunk_format", ["mapping", "to_dict", "attributes"])
@pytest.mark.parametrize(
    "usage_snapshots, empty_choices, expected",
    [
        pytest.param(
            [{"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}],
            False,
            {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            id="final-usage",
        ),
        pytest.param(
            [
                {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
                {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
                {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            ],
            False,
            {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            id="latest-cumulative-not-summed",
        ),
        pytest.param(
            [
                {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            ],
            False,
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            id="explicit-zero-overwrites",
        ),
        pytest.param(
            [{"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}],
            True,
            {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            id="usage-only-empty-choices",
        ),
        pytest.param(
            [SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9)],
            False,
            {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            id="attribute-usage-object",
        ),
        pytest.param(
            [{"prompt_tokens": 0, "completion_tokens": None}],
            False,
            {"input_tokens": 0},
            id="partial-usage-no-invented-counts",
        ),
        pytest.param(
            [None, {}],
            False,
            {},
            id="missing-null-empty-usage",
        ),
    ],
)
def test_stream_usage_is_exported(
    monkeypatch, async_stream, chunk_format, usage_snapshots, empty_choices, expected
):
    """Export final cumulative counts without changing chunks or output text."""
    # Keep LiteLLM's import-time model-cost lookup offline as well as the stream.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from traceai_litellm import (
        AsyncStreamingIteratorWrapper,
        StreamingIteratorWrapper,
    )

    payloads = [{"choices": [{"delta": {"content": "hello"}}]}]
    payloads.extend(
        {
            "choices": [] if empty_choices else [{"delta": {}}],
            "usage": usage,
        }
        for usage in usage_snapshots
    )
    # A later chunk without usage must not erase the last reported counts.
    payloads.append({"choices": [{"delta": {"content": "!"}}], "usage": None})
    payloads.append({"choices": [{"delta": {}}]})
    if chunk_format == "to_dict":
        chunks = [
            Mock(spec=["to_dict"], to_dict=Mock(return_value=payload))
            for payload in payloads
        ]
    elif chunk_format == "attributes":
        chunks = [SimpleNamespace(**payload) for payload in payloads]
    else:
        chunks = payloads

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    span = provider.get_tracer(__name__).start_span("completion")
    span_cm = MagicMock(spec=["__exit__"])
    span_cm.__exit__.side_effect = lambda *_: span.end()

    try:
        if async_stream:
            async def consume():
                async def source():
                    for chunk in chunks:
                        yield chunk

                wrapper = AsyncStreamingIteratorWrapper(source(), span, span_cm)
                return [chunk async for chunk in wrapper]

            received = asyncio.run(consume())
        else:
            received = list(StreamingIteratorWrapper(iter(chunks), span, span_cm))

        assert len(received) == len(chunks)
        assert all(actual is original for actual, original in zip(received, chunks))
        span_cm.__exit__.assert_called_once_with(None, None, None)
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        assert finished[0].status.status_code == StatusCode.OK
        assert finished[0].attributes["output.value"] == "hello!"
        assert {
            name: value
            for name, value in finished[0].attributes.items()
            if name.startswith("gen_ai.usage.")
        } == {f"gen_ai.usage.{name}": value for name, value in expected.items()}
    finally:
        if span.is_recording():
            span.end()
        provider.shutdown()
