"""Typed SDK requests must reach the transport unchanged, including when masked."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import grpc
import pytest
from fi_instrumentation import TraceConfig
from google.cloud import aiplatform_v1 as v1
from google.cloud import aiplatform_v1beta1 as beta
from google.cloud.aiplatform_v1.services.prediction_service import (
    transports as stable_rpc,
)
from google.cloud.aiplatform_v1beta1.services.prediction_service import (
    transports as beta_rpc,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_vertexai import VertexAIInstrumentor, _wrapper


@pytest.fixture
def predict():
    provider = TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    instrumentor = VertexAIInstrumentor()

    def run(sdk, instances, *, asynchronous=False, config=None):
        instrumentor.instrument(
            tracer_provider=provider, config=config or TraceConfig()
        )
        transports = stable_rpc if sdk is v1 else beta_rpc
        req = sdk.PredictRequest.from_json(
            json.dumps(
                {
                    "endpoint": "synthetic",
                    "instances": instances,
                    "parameters": {"other": 17},
                }
            )
        )

        def serialize(msg):
            return sdk.PredictRequest.pb(msg).SerializeToString(deterministic=True)

        original = serialize(req)
        response = sdk.PredictResponse()
        if asynchronous:
            channel = Mock(spec=grpc.aio.Channel)
            channel._unary_unary_interceptors = []
            rpc = AsyncMock(return_value=response)
            channel.unary_unary.return_value = rpc
            client = sdk.PredictionServiceAsyncClient(
                transport=transports.PredictionServiceGrpcAsyncIOTransport(
                    channel=channel
                )
            )
            assert asyncio.run(client.predict(request=req)) is response
            wire = rpc.await_args.args[0]
        else:
            rpc = Mock(spec=grpc.UnaryUnaryMultiCallable, return_value=response)
            rpc.with_call.return_value = (response, Mock(spec=grpc.Call))
            channel = Mock(spec=grpc.Channel)
            channel.unary_unary.return_value = rpc
            client = sdk.PredictionServiceClient(
                transport=transports.PredictionServiceGrpcTransport(channel=channel)
            )
            assert client.predict(request=req) is response
            wire = (rpc.with_call.call_args or rpc.call_args).args[0]
        assert type(wire) is sdk.PredictRequest
        assert serialize(wire) == serialize(req) == original
        (span,) = memory.get_finished_spans()
        return span.attributes

    try:
        yield run
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("sdk", [v1, beta], ids=["v1", "v1beta1"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "instances,accepted",
    [
        ([{"prompt": "one"}], {0: "one"}),
        ([{"content": "one"}], {0: "one"}),
        ([{"content": "one"}, {"content": "two"}], {0: "one", 1: "two"}),
        (
            [
                {"prompt": "first", "content": "unused"},
                {"content": ""},
                {"content": 9},
                {"prompt": None, "content": "unused"},
                {},
                {"content": "last"},
            ],
            {0: "first", 5: "last"},
        ),
    ],
    ids=["prompt", "single", "multiple", "precedence-and-skips"],
)
def test_typed_content(predict, sdk, asynchronous, instances, accepted):
    attrs = predict(sdk, instances, asynchronous=asynchronous)
    assert attrs.get(_wrapper.INPUT_VALUE) == "\n".join(accepted.values())
    assert attrs.get(_wrapper.INPUT_MIME_TYPE) == "text/plain"
    for idx in range(len(instances)):
        prefix = f"{_wrapper.GEN_AI_INPUT_MESSAGES}.{idx}"
        text, role = (
            f"{prefix}.{_wrapper.MESSAGE_CONTENT}",
            f"{prefix}.{_wrapper.MESSAGE_ROLE}",
        )
        if idx in accepted:
            assert json.loads(attrs[text]) == accepted[idx]
            assert attrs[role] == "user"
        else:
            assert text not in attrs and role not in attrs


@pytest.mark.parametrize("sdk", [v1, beta], ids=["v1", "v1beta1"])
@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
def test_content_follows_typed_prompt_mask(predict, sdk, flag):
    config = TraceConfig(**{flag: True})
    attrs = predict(sdk, [{"content": "synthetic"}], config=config)
    assert attrs.get(_wrapper.INPUT_VALUE) == (
        "__REDACTED__" if flag == "hide_inputs" else None
    )
    assert _wrapper.INPUT_MIME_TYPE not in attrs
    prefix = f"{_wrapper.GEN_AI_INPUT_MESSAGES}.0"
    for key, value in [
        (f"{prefix}.{_wrapper.MESSAGE_CONTENT}", json.dumps("synthetic")),
        (f"{prefix}.{_wrapper.MESSAGE_ROLE}", "user"),
    ]:
        assert attrs.get(key) == config.mask(key, value)
