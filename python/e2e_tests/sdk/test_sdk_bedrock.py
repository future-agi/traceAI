"""
E2E Tests for AWS Bedrock SDK Instrumentation

Tests Bedrock instrumentation via boto3. Requires AWS credentials.
"""

import pytest
import json
import time

from config import config, skip_if_no_bedrock


@pytest.fixture(scope="module")
def bedrock_client():
    """Create an instrumented Bedrock client."""
    if not config.has_bedrock():
        pytest.skip("AWS credentials not set")

    from fi_instrumentation import register
    try:
        from traceai_bedrock import BedrockInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_bedrock not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    BedrockInstrumentor().instrument(tracer_provider=tracer_provider)

    import boto3

    client = boto3.client(
        "bedrock-runtime",
        region_name=config.aws_bedrock_region,
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
    )

    yield client

    BedrockInstrumentor().uninstrument()


@skip_if_no_bedrock
class TestBedrockConverse:
    """Test Bedrock Converse API instrumentation."""

    def test_basic_converse(self, bedrock_client):
        """Test basic converse call."""
        response = bedrock_client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": "Say 'Hello E2E Test' in exactly 3 words."}
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 50},
        )

        assert response["output"]["message"]["content"][0]["text"] is not None
        time.sleep(2)
        print(f"Response: {response['output']['message']['content'][0]['text']}")

    def test_converse_with_system(self, bedrock_client):
        """Test converse with system message."""
        response = bedrock_client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            system=[{"text": "You are a helpful assistant that responds briefly."}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": "What is 2+2?"}],
                }
            ],
            inferenceConfig={"maxTokens": 20},
        )

        output_text = response["output"]["message"]["content"][0]["text"]
        assert output_text is not None
        assert "4" in output_text

    def test_converse_stream(self, bedrock_client):
        """Test streaming converse."""
        response = bedrock_client.converse_stream(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[
                {
                    "role": "user",
                    "content": [{"text": "Count from 1 to 5."}],
                }
            ],
            inferenceConfig={"maxTokens": 50},
        )

        chunks = []
        for event in response["stream"]:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    chunks.append(delta["text"])

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_tool_calling(self, bedrock_client):
        """Test tool/function calling via converse."""
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": "get_weather",
                        "description": "Get weather for a location",
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {
                                    "location": {
                                        "type": "string",
                                        "description": "City name",
                                    }
                                },
                                "required": ["location"],
                            }
                        },
                    }
                }
            ]
        }

        response = bedrock_client.converse(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            messages=[
                {
                    "role": "user",
                    "content": [{"text": "What's the weather in Paris?"}],
                }
            ],
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": 200},
        )

        content = response["output"]["message"]["content"]
        tool_uses = [c for c in content if "toolUse" in c]
        if tool_uses:
            assert tool_uses[0]["toolUse"]["name"] == "get_weather"
            print(f"Tool call: {tool_uses[0]['toolUse']['name']}")


@skip_if_no_bedrock
class TestBedrockErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, bedrock_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            bedrock_client.converse(
                modelId="invalid.model-id",
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": "test"}],
                    }
                ],
            )
