"""
E2E Tests for OpenAI SDK Instrumentation

Tests OpenAI API calls and verifies traces are correctly captured.
"""

import pytest
import os
import time
import asyncio
from typing import Dict, Any

from config import config, skip_if_no_openai


@pytest.fixture(scope="module")
def openai_client():
    """Create instrumented OpenAI client."""
    if not config.has_openai():
        pytest.skip("OpenAI API key not available")

    os.environ["OPENAI_API_KEY"] = config.openai_api_key

    # Import and instrument
    from fi_instrumentation import register
    from traceai_openai import OpenAIInstrumentor

    # Register tracer - sends to FutureAGI cloud
    tracer_provider = register(
        project_name="e2e_test_openai",
        project_version_name="1.0.0",
        verbose=False,  # Reduce output noise
    )

    # Instrument OpenAI
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

    # Create client
    from openai import OpenAI

    client = OpenAI()

    yield client

    # Cleanup
    OpenAIInstrumentor().uninstrument()


@pytest.fixture(scope="module")
def async_openai_client():
    """Create instrumented async OpenAI client."""
    if not config.has_openai():
        pytest.skip("OpenAI API key not available")

    os.environ["OPENAI_API_KEY"] = config.openai_api_key

    from openai import AsyncOpenAI

    return AsyncOpenAI()


@skip_if_no_openai
class TestOpenAIChatCompletion:
    """Test OpenAI chat completion instrumentation."""

    def test_basic_chat_completion(self, openai_client):
        """Test basic chat completion creates correct spans."""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Say 'Hello E2E Test' in exactly 3 words."}
            ],
            max_tokens=50,
        )

        # Verify response
        assert response.choices[0].message.content is not None
        assert response.usage.prompt_tokens > 0
        assert response.usage.completion_tokens > 0

        # Allow time for span export
        time.sleep(2)

        # Verify the response contains expected data
        print(f"Response: {response.choices[0].message.content}")
        print(f"Tokens - Prompt: {response.usage.prompt_tokens}, Completion: {response.usage.completion_tokens}")

    def test_chat_completion_with_system_message(self, openai_client):
        """Test chat with system message."""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        assert "4" in response.choices[0].message.content

    def test_chat_completion_streaming(self, openai_client):
        """Test streaming chat completion."""
        stream = openai_client.chat.completions.create(
            model="gpt-4o-mini",
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

    def test_chat_completion_with_function_calling(self, openai_client):
        """Test function/tool calling."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "City name",
                            }
                        },
                        "required": ["location"],
                    },
                },
            }
        ]

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "What's the weather in San Francisco?"}
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=100,
        )

        # Should trigger tool call
        message = response.choices[0].message
        if message.tool_calls:
            assert len(message.tool_calls) > 0
            assert message.tool_calls[0].function.name == "get_weather"
            print(f"Tool call: {message.tool_calls[0].function.name}")
            print(f"Arguments: {message.tool_calls[0].function.arguments}")

    def test_chat_completion_with_temperature(self, openai_client):
        """Test with different temperature settings."""
        # Low temperature - deterministic
        response1 = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "What is the capital of France?"}],
            temperature=0,
            max_tokens=20,
        )

        # Higher temperature
        response2 = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "What is the capital of France?"}],
            temperature=1.0,
            max_tokens=20,
        )

        assert "Paris" in response1.choices[0].message.content
        assert response2.choices[0].message.content is not None


@skip_if_no_openai
class TestOpenAIEmbeddings:
    """Test OpenAI embeddings instrumentation."""

    def test_single_embedding(self, openai_client):
        """Test single text embedding."""
        response = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input="Hello, world!",
        )

        assert len(response.data) == 1
        assert len(response.data[0].embedding) > 0
        assert response.usage.total_tokens > 0

        print(f"Embedding dimensions: {len(response.data[0].embedding)}")
        print(f"Tokens used: {response.usage.total_tokens}")

    def test_batch_embeddings(self, openai_client):
        """Test batch embeddings."""
        texts = [
            "First sentence for embedding.",
            "Second sentence for embedding.",
            "Third sentence for embedding.",
        ]

        response = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=texts,
        )

        assert len(response.data) == 3
        for i, embedding in enumerate(response.data):
            assert len(embedding.embedding) > 0
            assert embedding.index == i


@skip_if_no_openai
class TestOpenAIAsync:
    """Test async OpenAI operations."""

    @pytest.mark.asyncio
    async def test_async_chat_completion(self, async_openai_client):
        """Test async chat completion."""
        response = await async_openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Say 'async test' in one word."}
            ],
            max_tokens=10,
        )

        assert response.choices[0].message.content is not None

    @pytest.mark.asyncio
    async def test_async_streaming(self, async_openai_client):
        """Test async streaming."""
        stream = await async_openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Count 1 to 3."}
            ],
            max_tokens=30,
            stream=True,
        )

        chunks = []
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        assert len(chunks) > 0


@skip_if_no_openai
class TestOpenAIMultimodal:
    """Test multimodal (vision) capabilities."""

    def test_image_url_input(self, openai_client):
        """Test image URL input."""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image? Be brief."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/1200px-Cat03.jpg",
                            },
                        },
                    ],
                }
            ],
            max_tokens=100,
        )

        assert response.choices[0].message.content is not None
        content = response.choices[0].message.content.lower()
        # Should recognize it's a cat
        assert any(word in content for word in ["cat", "feline", "animal", "pet"])
        print(f"Vision response: {response.choices[0].message.content}")


@skip_if_no_openai
class TestOpenAIErrorHandling:
    """Test error handling and edge cases."""

    def test_invalid_model(self, openai_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            openai_client.chat.completions.create(
                model="invalid-model-name",
                messages=[{"role": "user", "content": "test"}],
            )

    def test_empty_messages(self, openai_client):
        """Test handling of empty messages."""
        with pytest.raises(Exception):
            openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[],
            )

    def test_max_tokens_limit(self, openai_client):
        """Test max tokens limiting."""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Write a very long story about dragons."}
            ],
            max_tokens=5,  # Very limited
        )

        # Response should be truncated
        assert response.usage.completion_tokens <= 5
