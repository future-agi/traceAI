"""
E2E Tests for MistralAI SDK Instrumentation

Tests MistralAI native SDK with tracing. Requires MISTRAL_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_mistral


@pytest.fixture(scope="module")
def mistral_client():
    """Create an instrumented Mistral client."""
    if not config.has_mistral():
        pytest.skip("MISTRAL_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_mistralai import MistralAIInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_mistralai not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    MistralAIInstrumentor().instrument(tracer_provider=tracer_provider)

    from mistralai import Mistral

    client = Mistral(api_key=config.mistral_api_key)

    yield client

    MistralAIInstrumentor().uninstrument()


@skip_if_no_mistral
class TestMistralChatCompletion:
    """Test Mistral chat completion instrumentation."""

    def test_basic_chat(self, mistral_client):
        """Test basic chat completion."""
        response = mistral_client.chat.complete(
            model="mistral-small-latest",
            messages=[
                {"role": "user", "content": "Say 'Hello E2E Test' in exactly 3 words."}
            ],
            max_tokens=50,
        )

        assert response.choices[0].message.content is not None
        assert response.usage.prompt_tokens > 0
        assert response.usage.completion_tokens > 0

        time.sleep(2)
        print(f"Response: {response.choices[0].message.content}")

    def test_streaming(self, mistral_client):
        """Test streaming chat completion."""
        stream = mistral_client.chat.stream(
            model="mistral-small-latest",
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
            max_tokens=50,
        )

        chunks = []
        for event in stream:
            if event.data.choices[0].delta.content:
                chunks.append(event.data.choices[0].delta.content)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_system_message(self, mistral_client):
        """Test with system + user messages."""
        response = mistral_client.chat.complete(
            model="mistral-small-latest",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        assert "4" in response.choices[0].message.content

    def test_tool_calling(self, mistral_client):
        """Test function/tool calling."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string", "description": "City name"}
                        },
                        "required": ["location"],
                    },
                },
            }
        ]

        response = mistral_client.chat.complete(
            model="mistral-small-latest",
            messages=[
                {"role": "user", "content": "What's the weather in Paris?"}
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=100,
        )

        message = response.choices[0].message
        if message.tool_calls:
            assert len(message.tool_calls) > 0
            print(f"Tool call: {message.tool_calls[0].function.name}")


@skip_if_no_mistral
class TestMistralErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, mistral_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            mistral_client.chat.complete(
                model="invalid-model-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
