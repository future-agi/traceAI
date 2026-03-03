"""
E2E Tests for Together SDK Instrumentation

Tests Together native SDK with tracing. Requires TOGETHER_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_together


@pytest.fixture(scope="module")
def together_client():
    """Create an instrumented Together client."""
    if not config.has_together():
        pytest.skip("TOGETHER_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_together import TogetherInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_together not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    TogetherInstrumentor().instrument(tracer_provider=tracer_provider)

    import together

    client = together.Together(api_key=config.together_api_key)

    yield client

    TogetherInstrumentor().uninstrument()


@skip_if_no_together
class TestTogetherChatCompletion:
    """Test Together chat completion instrumentation."""

    def test_basic_chat(self, together_client):
        """Test basic chat completion."""
        response = together_client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
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

    def test_streaming(self, together_client):
        """Test streaming chat completion."""
        stream = together_client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
            max_tokens=50,
            stream=True,
        )

        chunks = []
        for chunk in stream:
            if chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_system_message(self, together_client):
        """Test with system + user messages."""
        response = together_client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        assert "4" in response.choices[0].message.content

    def test_tool_calling(self, together_client):
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

        response = together_client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            messages=[
                {"role": "user", "content": "What's the weather in Tokyo?"}
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=100,
        )

        message = response.choices[0].message
        if message.tool_calls:
            assert len(message.tool_calls) > 0
            print(f"Tool call: {message.tool_calls[0].function.name}")


@skip_if_no_together
class TestTogetherErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, together_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            together_client.chat.completions.create(
                model="invalid-model-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
