"""
E2E Tests for DeepSeek SDK Instrumentation

Tests DeepSeek instrumentation using Google's OpenAI-compatible endpoint.
The DeepSeek instrumentor wraps the openai.OpenAI SDK methods.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def deepseek_client():
    """Create an instrumented DeepSeek (OpenAI-compat) client via Google endpoint."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_deepseek import DeepSeekInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_deepseek not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    DeepSeekInstrumentor().instrument(tracer_provider=tracer_provider)

    from openai import OpenAI

    client = OpenAI(
        base_url=config.google_openai_base_url,
        api_key=config.google_api_key,
    )

    yield client

    DeepSeekInstrumentor().uninstrument()


@pytest.fixture(scope="module")
def async_deepseek_client():
    """Create an async DeepSeek (OpenAI-compat) client via Google endpoint."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=config.google_openai_base_url,
        api_key=config.google_api_key,
    )


@skip_if_no_google
class TestDeepSeekChatCompletion:
    """Test DeepSeek chat completion instrumentation."""

    def test_basic_chat(self, deepseek_client):
        """Test basic chat completion."""
        response = deepseek_client.chat.completions.create(
            model=config.google_model,
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

    def test_streaming(self, deepseek_client):
        """Test streaming chat completion."""
        stream = deepseek_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
            max_tokens=50,
            stream=True,
        )

        chunks = []
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_tool_calling(self, deepseek_client):
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

        response = deepseek_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "What's the weather in London?"}
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=100,
        )

        message = response.choices[0].message
        if message.tool_calls:
            assert len(message.tool_calls) > 0
            assert message.tool_calls[0].function.name == "get_weather"
            print(f"Tool call: {message.tool_calls[0].function.name}")

    def test_system_message(self, deepseek_client):
        """Test with system + user messages."""
        response = deepseek_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        assert "4" in response.choices[0].message.content

    def test_multi_turn_conversation(self, deepseek_client):
        """Test multi-turn conversation."""
        response = deepseek_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "My name is Bob."},
                {"role": "assistant", "content": "Nice to meet you, Bob!"},
                {"role": "user", "content": "What's my name?"},
            ],
            max_tokens=30,
        )

        assert "Bob" in response.choices[0].message.content


@skip_if_no_google
class TestDeepSeekAsync:
    """Test async DeepSeek operations."""

    @pytest.mark.asyncio
    async def test_async_chat(self, async_deepseek_client):
        """Test async chat completion."""
        response = await async_deepseek_client.chat.completions.create(
            model=config.google_model,
            messages=[
                {"role": "user", "content": "Say 'async test' in one word."}
            ],
            max_tokens=10,
        )

        assert response.choices[0].message.content is not None


@skip_if_no_google
class TestDeepSeekErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, deepseek_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            deepseek_client.chat.completions.create(
                model="invalid-model-name-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
