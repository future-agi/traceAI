"""T4-PY-CLOUD-007: InvokeModel streams retain their spans until consumption.

These tests use a fake botocore EventStream and an in-memory OTel exporter only.
"""

import io
import json
from unittest.mock import Mock

import pytest
from botocore.eventstream import EventStream
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_bedrock._wrappers import _InvokeModelWithResponseStream


class FakeEventStream(EventStream):
    def __init__(self, events, error=None, close_error=None):
        # No HTTP response, parser, client, credentials, or network is required.
        self.events = events
        self.error = error
        self.close_error = close_error
        self.consumed = 0
        self.close_calls = 0

    def __iter__(self):
        for event in self.events:
            self.consumed += 1
            yield event
        if self.error is not None:
            raise self.error

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        return "closed"


def chunk(payload):
    return {"chunk": {"bytes": json.dumps(payload).encode()}}


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer(__name__), exporter
    provider.shutdown()


def invoke(tracing, stream, model="meta.llama3-8b-instruct-v1:0", body=None):
    tracer, exporter = tracing
    if body is None:
        body = '{"prompt":"hello","max_gen_len":16}'
    kwargs = {"modelId": model, "contentType": "application/json", "body": body}
    response = {"body": stream, "contentType": "application/json"}
    wrapped = Mock(return_value=response)
    result = _InvokeModelWithResponseStream(tracer)(wrapped)(**kwargs)
    wrapped.assert_called_once_with(**kwargs)
    assert result is response
    assert result["contentType"] == "application/json"
    return result["body"], exporter


@pytest.mark.parametrize(
    "model,payloads",
    [
        (
            "meta.llama3-8b-instruct-v1:0",
            [
                {"generation": "Hello", "prompt_token_count": 3, "generation_token_count": 1},
                {"generation": " world", "prompt_token_count": None, "generation_token_count": 2},
                {"generation": "", "stop_reason": "stop"},
            ],
        ),
        (
            "us.meta.llama3-2-3b-instruct-v1:0",
            [
                {"generation": "Hello", "prompt_token_count": 3, "generation_token_count": 1},
                {"generation": " world", "generation_token_count": 2},
            ],
        ),
        (
            "amazon.titan-text-express-v1",
            [
                {"outputText": "Hello", "inputTextTokenCount": 3, "totalOutputTextTokenCount": 1},
                {"outputText": " world", "totalOutputTextTokenCount": 2},
            ],
        ),
        (
            "cohere.command-text-v14",
            [
                {"text": "Hello", "is_finished": False},
                {"text": " world", "is_finished": False},
                {
                    "is_finished": True,
                    "amazon-bedrock-invocationMetrics": {"inputTokenCount": 3, "outputTokenCount": 2},
                },
            ],
        ),
        (
            "mistral.mistral-7b-instruct-v0:2",
            [
                {"outputs": [{"text": "Hello"}]},
                {
                    "outputs": [{"text": " world", "stop_reason": "stop"}],
                    "amazon-bedrock-invocationMetrics": {"inputTokenCount": 3, "outputTokenCount": 2},
                },
            ],
        ),
    ],
    ids=["llama", "llama-profile", "titan", "cohere", "mistral"],
)
def test_non_anthropic_stream_defers_and_captures_output_and_usage(tracing, model, payloads):
    events = [chunk(payload) for payload in payloads]
    source = FakeEventStream(events)
    stream, exporter = invoke(tracing, source, model)
    assert source.consumed == 0
    assert exporter.get_finished_spans() == ()

    iterator = iter(stream)
    assert next(iterator) is events[0]
    assert exporter.get_finished_spans() == ()
    remaining = list(iterator)
    assert all(actual is expected for actual, expected in zip(remaining, events[1:]))
    assert len(remaining) == len(events) - 1
    span, = exporter.get_finished_spans()
    attrs = span.attributes
    assert attrs["gen_ai.request.model"] == model
    assert attrs["output.value"] == "Hello world"
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.total_tokens"] == 5
    assert json.loads(attrs["input.value"]) == {"prompt": "hello", "max_gen_len": 16}
    assert json.loads(attrs["gen_ai.request.parameters"])["max_gen_len"] == 16
    assert span.status.status_code == StatusCode.OK
    # Both another exhaustion and a close must be safe after finalization.
    with pytest.raises(StopIteration):
        next(iterator)
    assert stream.close() == "closed"
    assert source.close_calls == 1
    assert len(exporter.get_finished_spans()) == 1


def test_stream_error_is_reraised_and_records_partial_output(tracing):
    error = RuntimeError("fake stream failure")
    event = chunk({"generation": "partial", "prompt_token_count": 3})
    source = FakeEventStream([event], error=error)
    stream, exporter = invoke(tracing, source)
    assert exporter.get_finished_spans() == ()
    iterator = iter(stream)
    assert next(iterator) is event
    with pytest.raises(RuntimeError) as caught:
        next(iterator)
    assert caught.value is error
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["output.value"] == "partial"
    assert span.attributes["gen_ai.usage.input_tokens"] == 3
    assert span.events[0].attributes["exception.type"] == "RuntimeError"
    stream.close()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("consume_first", [False, True], ids=["unread", "partial"])
def test_explicit_close_finishes_without_draining(tracing, consume_first):
    events = [chunk({"generation": "first"}), chunk({"generation": "second"})]
    source = FakeEventStream(events)
    stream, exporter = invoke(tracing, source)
    assert exporter.get_finished_spans() == ()
    if consume_first:
        assert next(iter(stream)) is events[0]
    assert stream.close() == "closed"
    assert source.consumed == int(consume_first)
    assert source.close_calls == 1
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.request.model"] == "meta.llama3-8b-instruct-v1:0"
    if consume_first:
        assert span.attributes["output.value"] == "first"
    stream.close()
    assert len(exporter.get_finished_spans()) == 1


def test_close_error_is_preserved_and_recorded(tracing):
    error = OSError("fake close failure")
    stream, exporter = invoke(tracing, FakeEventStream([], close_error=error))
    assert exporter.get_finished_spans() == ()
    with pytest.raises(OSError) as caught:
        stream.close()
    assert caught.value is error
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.events


def test_empty_stream_records_request_and_finishes(tracing):
    stream, exporter = invoke(tracing, FakeEventStream([]))
    assert exporter.get_finished_spans() == ()
    assert list(stream) == []
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.request.model"] == "meta.llama3-8b-instruct-v1:0"
    assert json.loads(span.attributes["input.value"])["prompt"] == "hello"


def test_zero_counts_and_terminal_metrics_are_not_summed(tracing):
    source = FakeEventStream([
        chunk({"generation": "", "prompt_token_count": 0, "generation_token_count": 0}),
        chunk({"amazon-bedrock-invocationMetrics": {"inputTokenCount": 0, "outputTokenCount": 0}}),
    ])
    stream, exporter = invoke(tracing, source)
    list(stream)
    span, = exporter.get_finished_spans()
    assert span.attributes["output.value"] == ""
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        assert span.attributes[f"gen_ai.usage.{name}"] == 0


def test_bad_chunks_do_not_change_stream_results(tracing):
    events = [
        {"chunk": {"bytes": b"not JSON"}},
        chunk(["unexpected", "schema"]),
        chunk({"generation": "valid"}),
    ]
    stream, exporter = invoke(tracing, FakeEventStream(events))
    actual = list(stream)
    assert len(actual) == len(events)
    assert all(left is right for left, right in zip(actual, events))
    span, = exporter.get_finished_spans()
    assert span.attributes["output.value"] == "valid"


@pytest.mark.parametrize("body", [b'{"prompt":"hello"}', io.BytesIO(b'{"prompt":"hello"}')])
def test_bytes_and_file_request_bodies_are_not_consumed_by_instrumentation(tracing, body):
    stream, exporter = invoke(tracing, FakeEventStream([chunk({"generation": "ok"})]), body=body)
    assert exporter.get_finished_spans() == ()
    if hasattr(body, "tell"):
        assert body.tell() == 0
    list(stream)
    span, = exporter.get_finished_spans()
    assert span.attributes["output.value"] == "ok"


def test_invocation_error_ends_span_and_preserves_exception(tracing):
    tracer, exporter = tracing
    error = RuntimeError("fake invocation failure")
    wrapped = Mock(side_effect=error)
    with pytest.raises(RuntimeError) as caught:
        _InvokeModelWithResponseStream(tracer)(wrapped)(
            modelId="meta.llama3-8b-instruct-v1:0", body='{"prompt":"hello"}'
        )
    assert caught.value is error
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["gen_ai.request.model"] == "meta.llama3-8b-instruct-v1:0"
    assert len(span.events) == 1


def test_unknown_schema_output_is_serialized(tracing):
    payload = {"result": {"content": [{"text": "hello"}]}}
    stream, exporter = invoke(tracing, FakeEventStream([chunk(payload)]), model="custom-model")
    assert exporter.get_finished_spans() == ()
    list(stream)
    span, = exporter.get_finished_spans()
    assert json.loads(span.attributes["output.value"]) == [payload]
    assert span.attributes["output.mime_type"] == "application/json"


def test_existing_messages_stream_remains_deferred(tracing):
    events = [
        chunk({
            "type": "message_start",
            "message": {
                "id": "fake-message",
                "type": "message",
                "role": "assistant",
                "model": "fake-model",
                "content": [],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        }),
        chunk({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "hello"}}),
        chunk({"type": "message_stop"}),
    ]
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hello"}],
    })
    stream, exporter = invoke(tracing, FakeEventStream(events), model="anthropic.fake-model", body=body)
    assert exporter.get_finished_spans() == ()
    assert list(stream) == events
    span, = exporter.get_finished_spans()
    output = json.loads(span.attributes["output.value"])
    assert output["content"][0]["text"] == "hello"
    assert span.attributes["gen_ai.request.model"] == "anthropic.fake-model"
    assert span.attributes["gen_ai.usage.input_tokens"] == 3


def test_suppressed_instrumentation_keeps_original_stream(tracing):
    source = FakeEventStream([])
    token = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        stream, exporter = invoke(tracing, source)
    finally:
        context_api.detach(token)
    assert stream is source
    assert exporter.get_finished_spans() == ()
