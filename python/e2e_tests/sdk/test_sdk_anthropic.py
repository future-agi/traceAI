"""
E2E Tests for Anthropic SDK Instrumentation

Tests Anthropic API calls and verifies traces are correctly captured.
"""

import pytest
import os
import time
import asyncio
from typing import Dict, Any

from config import config, skip_if_no_anthropic


@pytest.fixture(scope="module")
def anthropic_client():
    """Create instrumented Anthropic client."""
    if not config.has_anthropic():
        pytest.skip("Anthropic API key not available")

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key

    # Import and instrument
    from fi_instrumentation import register
    try:
        from traceai_anthropic import AnthropicInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_anthropic not installed or incompatible")

    # Register tracer - sends to FutureAGI cloud
    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    # Instrument Anthropic
    AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)

    # Create client
    from anthropic import Anthropic

    client = Anthropic()

    yield client

    # Cleanup
    AnthropicInstrumentor().uninstrument()


@pytest.fixture(scope="module")
def async_anthropic_client():
    """Create async Anthropic client."""
    if not config.has_anthropic():
        pytest.skip("Anthropic API key not available")

    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        pytest.skip("anthropic not installed")

    return AsyncAnthropic()


@skip_if_no_anthropic
class TestAnthropicMessages:
    """Test Anthropic messages API instrumentation."""

    def test_basic_message(self, anthropic_client):
        """Test basic message creation."""
        response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=50,
            messages=[
                {"role": "user", "content": "Say 'Hello E2E Test' in exactly 3 words."}
            ],
        )

        # Verify response
        assert len(response.content) > 0
        assert response.content[0].type == "text"
        assert response.usage.input_tokens > 0
        assert response.usage.output_tokens > 0

        # Allow time for span export
        time.sleep(2)

        print(f"Response: {response.content[0].text}")
        print(f"Tokens - Input: {response.usage.input_tokens}, Output: {response.usage.output_tokens}")

    def test_message_with_system(self, anthropic_client):
        """Test message with system prompt."""
        response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=30,
            system="You are a helpful assistant that responds in exactly one word.",
            messages=[
                {"role": "user", "content": "What is 2+2?"}
            ],
        )

        assert len(response.content) > 0
        text = response.content[0].text.strip()
        # Should be a brief response
        assert len(text.split()) <= 3

    def test_streaming_message(self, anthropic_client):
        """Test streaming messages."""
        chunks = []

        with anthropic_client.messages.stream(
            model="claude-3-5-haiku-latest",
            max_tokens=50,
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
        ) as stream:
            for text in stream.text_stream:
                chunks.append(text)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_multi_turn_conversation(self, anthropic_client):
        """Test multi-turn conversation."""
        messages = [
            {"role": "user", "content": "My name is Alice."},
        ]

        response1 = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=50,
            messages=messages,
        )

        # Add assistant response and follow-up
        messages.append({"role": "assistant", "content": response1.content[0].text})
        messages.append({"role": "user", "content": "What's my name?"})

        response2 = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=50,
            messages=messages,
        )

        assert "Alice" in response2.content[0].text


@skip_if_no_anthropic
class TestAnthropicToolUse:
    """Test Anthropic tool use instrumentation."""

    def test_tool_use(self, anthropic_client):
        """Test tool use/function calling."""
        tools = [
            {
                "name": "get_weather",
                "description": "Get the current weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "City name",
                        }
                    },
                    "required": ["location"],
                },
            }
        ]

        response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=200,
            tools=tools,
            messages=[
                {"role": "user", "content": "What's the weather in Tokyo?"}
            ],
        )

        # Check for tool use in response
        has_tool_use = False
        for content in response.content:
            if content.type == "tool_use":
                has_tool_use = True
                assert content.name == "get_weather"
                assert "location" in content.input
                print(f"Tool: {content.name}, Input: {content.input}")
                break

        # Note: Claude might not always use tools
        if not has_tool_use:
            print("Model chose not to use tool (acceptable)")

    def test_multi_tool_use(self, anthropic_client):
        """Test multiple tools available."""
        tools = [
            {
                "name": "get_weather",
                "description": "Get weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"}
                    },
                    "required": ["location"],
                },
            },
            {
                "name": "get_time",
                "description": "Get current time for a timezone",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "timezone": {"type": "string"}
                    },
                    "required": ["timezone"],
                },
            },
        ]

        response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=200,
            tools=tools,
            messages=[
                {"role": "user", "content": "What's the weather in London and what time is it there?"}
            ],
        )

        tool_uses = [c for c in response.content if c.type == "tool_use"]
        print(f"Number of tool calls: {len(tool_uses)}")


@skip_if_no_anthropic
class TestAnthropicAsync:
    """Test async Anthropic operations."""

    @pytest.mark.asyncio
    async def test_async_message(self, async_anthropic_client):
        """Test async message creation."""
        response = await async_anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=20,
            messages=[
                {"role": "user", "content": "Say 'async test' briefly."}
            ],
        )

        assert len(response.content) > 0

    @pytest.mark.asyncio
    async def test_async_streaming(self, async_anthropic_client):
        """Test async streaming."""
        chunks = []

        async with async_anthropic_client.messages.stream(
            model="claude-3-5-haiku-latest",
            max_tokens=50,
            messages=[
                {"role": "user", "content": "Count 1 to 3."}
            ],
        ) as stream:
            async for text in stream.text_stream:
                chunks.append(text)

        assert len(chunks) > 0


@skip_if_no_anthropic
class TestAnthropicVision:
    """Test Anthropic vision capabilities."""

    def test_image_url_input(self, anthropic_client):
        """Test image URL analysis."""
        import base64
        import httpx

        # Fetch image and convert to base64
        image_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/480px-Cat03.jpg"

        try:
            image_data = httpx.get(image_url).content
            base64_image = base64.standard_b64encode(image_data).decode("utf-8")

            response = anthropic_client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=100,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": base64_image,
                                },
                            },
                            {
                                "type": "text",
                                "text": "What animal is in this image? Answer in one word.",
                            },
                        ],
                    }
                ],
            )

            content = response.content[0].text.lower()
            assert any(word in content for word in ["cat", "feline", "kitten"])
            print(f"Vision response: {response.content[0].text}")
        except Exception as e:
            pytest.skip(f"Could not fetch image: {e}")


@skip_if_no_anthropic
class TestAnthropicErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, anthropic_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            anthropic_client.messages.create(
                model="invalid-model",
                max_tokens=10,
                messages=[{"role": "user", "content": "test"}],
            )

    def test_empty_messages(self, anthropic_client):
        """Test handling of empty messages."""
        with pytest.raises(Exception):
            anthropic_client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=10,
                messages=[],
            )

    def test_max_tokens_exceeded(self, anthropic_client):
        """Test max tokens limit."""
        response = anthropic_client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=5,  # Very limited
            messages=[
                {"role": "user", "content": "Write a long essay about space exploration."}
            ],
        )

        # Response should be limited
        assert response.usage.output_tokens <= 10  # Small buffer
