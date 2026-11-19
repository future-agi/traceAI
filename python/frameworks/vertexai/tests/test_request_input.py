"""Exercise the legacy prediction client with a stub gRPC channel and exported spans."""

import json
from unittest.mock import Mock

import grpc
import pytest
from fi_instrumentation import TraceConfig
from google.cloud import aiplatform_v1 as v1
from google.cloud.aiplatform_v1.services.prediction_service.transports.grpc import (
    PredictionServiceGrpcTransport,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_vertexai import VertexAIInstrumentor, _wrapper


@pytest.fixture
def exported_request():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = VertexAIInstrumentor()

    def run(contents, config):
        instrumentor.instrument(tracer_provider=provider, config=config)
        channel = Mock(spec=grpc.Channel)
        response = v1.GenerateContentResponse()
        rpc = Mock(spec=grpc.UnaryUnaryMultiCallable, return_value=response)
        rpc.with_call.return_value = (response, Mock(spec=grpc.Call))
        channel.unary_unary.return_value = rpc
        transport = PredictionServiceGrpcTransport(channel=channel)
        client = v1.PredictionServiceClient(transport=transport)
        request = v1.GenerateContentRequest(model="synthetic-model", contents=contents)
        assert client.generate_content(request=request) is response
        (span,) = exporter.get_finished_spans()
        return span.attributes

    try:
        yield run
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def turns():
    return [
        {"role": role, "parts": [{"text": text}]}
        for role, text in [("user", "first"), ("model", "second"), ("user", "third")]
    ]


@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
@pytest.mark.parametrize("enabled", [False, True], ids=["unset", "set"])
def test_exported_chat_input(exported_request, flag, enabled):
    config = TraceConfig(**({flag: True} if enabled else {}))
    attrs = exported_request(turns(), config)
    if enabled:
        assert _wrapper.INPUT_VALUE not in attrs
    else:
        assert json.loads(attrs[_wrapper.INPUT_VALUE]) == "first | second | third"


def test_image_only_request_keeps_its_json_and_image_uri(exported_request):
    uri = "gs://synthetic-example/image.jpg"
    parts = [{"file_data": {"mime_type": "image/jpeg", "file_uri": uri}}]
    contents = [{"role": "user", "parts": parts}]
    attrs = exported_request(contents, TraceConfig())
    expected = v1.GenerateContentRequest(model="synthetic-model", contents=contents)
    assert json.loads(attrs[_wrapper.INPUT_VALUE]) == type(expected).to_dict(expected)
    assert json.loads(attrs[_wrapper.INPUT_IMAGES]) == [uri]


def test_unreadable_contents_do_not_fall_back_to_raw_request(
    exported_request, monkeypatch
):
    monkeypatch.setattr(
        v1.GenerateContentRequest, "to_dict", lambda _: {"contents": [None]}
    )
    attrs = exported_request(turns(), TraceConfig())
    assert _wrapper.INPUT_VALUE not in attrs
