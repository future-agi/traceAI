"""valid non-text responses must not fail instrumentation."""

import json
from unittest.mock import Mock

import pytest
from google.cloud import aiplatform_v1 as v1
from google.cloud import aiplatform_v1beta1 as v1beta1
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_vertexai import _wrapper


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setattr(_wrapper._instrumentation_status, "_IS_INSTRUMENTED", True)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider.get_tracer(__name__), exporter
    finally:
        provider.shutdown()


def call_fake_rpc(tracer, request, response):
    # Only the protobuf schema is real: no SDK client, credentials or network.
    rpc = Mock(return_value=response)
    result = _wrapper._Wrapper(tracer)(rpc)(request=request)
    rpc.assert_called_once_with(request=request)
    assert result is response


@pytest.mark.parametrize("sdk", [v1, v1beta1], ids=["v1", "v1beta1"])
@pytest.mark.parametrize(
    "response_data",
    [
        pytest.param(
            {"candidates": [], "prompt_feedback": {"block_reason": "SAFETY"}},
            id="no-candidates",
        ),
        pytest.param(
            {"candidates": [{"content": {"role": "model", "parts": []},
                             "finish_reason": "SAFETY"}]},
            id="empty-parts",
        ),
        pytest.param(
            {"candidates": [{"content": {"role": "model", "parts": [
                {"function_call": {"name": "lookup", "args": {"city": "example"}}}
            ]}}]},
            id="tool-call",
        ),
        pytest.param(
            {"candidates": [{"content": {"role": "model", "parts": [
                {"inline_data": {"mime_type": "image/png", "data": b"fake-image"}}
            ]}}]},
            id="image",
        ),
    ],
)
def test_non_text_response_is_returned_and_span_ends(tracing, sdk, response_data):
    tracer, exporter = tracing
    response = sdk.GenerateContentResponse(
        **response_data, usage_metadata={"prompt_token_count": 2, "total_token_count": 2}
    )
    request = sdk.GenerateContentRequest(model="test-model")

    call_fake_rpc(tracer, request, response)

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.OK
    assert json.loads(span.attributes[_wrapper.OUTPUT_VALUE]) == type(response).to_dict(response)
    assert span.attributes[_wrapper.OUTPUT_MIME_TYPE] == _wrapper.JSON
    assert span.attributes[_wrapper.GEN_AI_USAGE_INPUT_TOKENS] == 2
    assert span.attributes[_wrapper.GEN_AI_USAGE_TOTAL_TOKENS] == 2
    assert not span.events


def test_text_response_keeps_existing_output_and_ends_span(tracing):
    tracer, exporter = tracing
    response = v1.GenerateContentResponse(
        candidates=[{"content": {"role": "model", "parts": [{"text": "Hello."}]}}]
    )

    call_fake_rpc(tracer, v1.GenerateContentRequest(model="test-model"), response)

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.OK
    assert span.attributes[_wrapper.OUTPUT_VALUE] == _wrapper.safe_json_dumps(
        _wrapper.safe_json_dumps("Hello.")
    )


@pytest.mark.parametrize("failure_stage", ["serialization", "structured-attributes"])
def test_response_extraction_failure_does_not_replace_result(
    tracing, monkeypatch, failure_stage
):
    tracer, exporter = tracing
    response = v1.GenerateContentResponse(
        candidates=[{"content": {"role": "model", "parts": [{"text": "Hello."}]}}]
    )
    failure = Mock(side_effect=RuntimeError("synthetic extraction failure"))
    if failure_stage == "serialization":
        monkeypatch.setattr(v1.GenerateContentResponse, "to_dict", failure)
    else:
        original = _wrapper._update_span

        def update_span(obj, span):
            if obj is response:
                return failure(obj, span)
            return original(obj, span)

        monkeypatch.setattr(_wrapper, "_update_span", update_span)

    call_fake_rpc(tracer, v1.GenerateContentRequest(model="test-model"), response)

    failure.assert_called_once()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.OK
    assert not span.events


def test_sdk_exception_is_not_replaced(tracing):
    tracer, exporter = tracing
    error = RuntimeError("synthetic SDK failure")
    rpc = Mock(side_effect=error)
    request = v1.GenerateContentRequest(model="test-model")

    with pytest.raises(RuntimeError) as caught:
        _wrapper._Wrapper(tracer)(rpc)(request=request)

    assert caught.value is error
    rpc.assert_called_once_with(request=request)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1
    assert span.events[0].name == "exception"


def test_finish_records_exception_results_and_ends_span(tracing):
    tracer, exporter = tracing
    span = tracer.start_span("generate_content")
    error = RuntimeError("synthetic callback failure")

    assert _wrapper._finish(span, error) is None

    assert not span.is_recording()
    (finished,) = exporter.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert len(finished.events) == 1
    assert finished.events[0].attributes["exception.message"] == str(error)
