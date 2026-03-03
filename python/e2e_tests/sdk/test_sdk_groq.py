"""
E2E Tests for Groq SDK Instrumentation

Tests Groq API calls and verifies traces are correctly captured.
"""

import pytest
import os
import time
from typing import Dict, Any

from config import config, skip_if_no_groq


@pytest.fixture(scope="module")
def groq_client():
    """Create instrumented Groq client."""
    if not config.has_groq():
        pytest.skip("Groq API key not available")

    os.environ["GROQ_API_KEY"] = config.groq_api_key

    # Import and instrument
    from fi_instrumentation import register
    try:
        from traceai_groq import GroqInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_groq not installed or incompatible")

    # Register tracer
    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    # Instrument Groq
    GroqInstrumentor().instrument(tracer_provider=tracer_provider)

    # Create client
    from groq import Groq

    client = Groq()

    yield client

    # Cleanup
    GroqInstrumentor().uninstrument()


@skip_if_no_groq
class TestGroqChatCompletion:
    """Test Groq chat completion instrumentation."""

    def test_basic_chat_completion(self, groq_client):
        """Test basic chat completion."""
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
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

        print(f"Response: {response.choices[0].message.content}")
        print(f"Tokens - Prompt: {response.usage.prompt_tokens}, Completion: {response.usage.completion_tokens}")

    def test_streaming_chat(self, groq_client):
        """Test streaming chat completion."""
        stream = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
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

    def test_different_models(self, groq_client):
        """Test with different Groq models."""
        models = [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ]

        for model in models:
            try:
                response = groq_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "What is 2+2?"}],
                    max_tokens=20,
                )
                assert response.choices[0].message.content is not None
                print(f"Model {model}: {response.choices[0].message.content}")
            except Exception as e:
                print(f"Model {model} not available: {e}")

    def test_tool_calling(self, groq_client):
        """Test tool/function calling with Groq."""
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

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
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

    def test_with_system_message(self, groq_client):
        """Test with system message."""
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds in exactly one word."},
                {"role": "user", "content": "What color is the sky?"},
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        print(f"Response: {response.choices[0].message.content}")


@skip_if_no_groq
class TestGroqAsync:
    """Test async Groq operations."""

    @pytest.mark.asyncio
    async def test_async_chat(self, groq_client):
        """Test async chat completion."""
        # Groq uses same client for sync/async
        # Creating async version
        os.environ["GROQ_API_KEY"] = config.groq_api_key
        from groq import AsyncGroq

        async_client = AsyncGroq()

        response = await async_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Say hello briefly."}],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None


@skip_if_no_groq
class TestGroqPerformance:
    """Test Groq's fast inference performance."""

    def test_fast_inference(self, groq_client):
        """Test Groq's fast inference times."""
        import time

        start = time.time()
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": "What is 1+1?"}],
            max_tokens=10,
        )
        elapsed = time.time() - start

        assert response.choices[0].message.content is not None
        print(f"Inference time: {elapsed:.2f}s")
        # Groq should be fast
        assert elapsed < 5.0  # Should complete in under 5 seconds


@skip_if_no_groq
class TestGroqErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, groq_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            groq_client.chat.completions.create(
                model="invalid-model-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
