"""Bedrock and SageMaker both hook boto3 client creation; each must wrap only its own services."""
import json

import pytest

from conftest import ENDPOINT, attrs, body_of, make_client, ok, respond

CLAUDE_REQUEST = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
}
CLAUDE_RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-haiku",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 2},
}


def drive(spans):
    runtime = make_client("sagemaker-runtime")
    bedrock = make_client("bedrock-runtime")
    respond(runtime, "InvokeEndpoint", ok({"generated_text": "from-sagemaker"}))
    respond(
        bedrock,
        "InvokeModel",
        {"body": body_of(CLAUDE_RESPONSE), "contentType": "application/json", "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {}}},
        service="bedrock-runtime",
    )
    runtime.invoke_endpoint(EndpointName=ENDPOINT, Body=json.dumps({"inputs": "x"}), ContentType="application/json")
    bedrock.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        body=json.dumps(CLAUDE_REQUEST),
        contentType="application/json",
    )
    return runtime, bedrock


@pytest.mark.parametrize("sagemaker_first", [True, False], ids=["sagemaker-first", "bedrock-first"])
def test_each_instrumentor_wraps_only_its_own_service(provider, sagemaker_first):
    from traceai_bedrock import BedrockInstrumentor
    from traceai_sagemaker import SageMakerInstrumentor

    sagemaker, bedrock = SageMakerInstrumentor(), BedrockInstrumentor()
    order = [sagemaker, bedrock] if sagemaker_first else [bedrock, sagemaker]
    for instrumentor in order:
        instrumentor.instrument(tracer_provider=provider)
    try:
        runtime, bedrock_client = drive(provider.exporter)
        by_name = {span.name: span for span in provider.exporter.get_finished_spans()}
        assert sorted(by_name) == ["bedrock.invoke_model", "sagemaker.invoke_endpoint"], sorted(by_name)
        assert attrs(by_name["sagemaker.invoke_endpoint"])["gen_ai.provider.name"] == "aws.sagemaker"
        assert attrs(by_name["sagemaker.invoke_endpoint"])["output.value"] == "from-sagemaker"
        # Neither instrumentor touched the other's client.
        assert not hasattr(runtime, "_unwrapped_invoke_model")
        assert not hasattr(bedrock_client, "_unwrapped_invoke_endpoint")
        assert not hasattr(make_client("s3"), "_unwrapped_invoke_endpoint")
    finally:
        for instrumentor in reversed(order):
            instrumentor.uninstrument()


def test_uninstrumenting_sagemaker_leaves_bedrock_working(provider):
    from traceai_bedrock import BedrockInstrumentor
    from traceai_sagemaker import SageMakerInstrumentor

    sagemaker, bedrock = SageMakerInstrumentor(), BedrockInstrumentor()
    sagemaker.instrument(tracer_provider=provider)
    bedrock.instrument(tracer_provider=provider)
    try:
        # Out of order: the inner (newer) wrapper is removed last in LIFO, here the outer one goes first.
        sagemaker.uninstrument()
        runtime = make_client("sagemaker-runtime")
        assert not hasattr(runtime, "_unwrapped_invoke_endpoint")
        client = make_client("bedrock-runtime")
        assert hasattr(client, "_unwrapped_invoke_model")
    finally:
        bedrock.uninstrument()
