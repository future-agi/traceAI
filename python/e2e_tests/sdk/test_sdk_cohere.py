"""
E2E Tests for Cohere SDK Instrumentation

Tests Cohere native SDK with tracing. Requires COHERE_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_cohere


@pytest.fixture(scope="module")
def cohere_client():
    """Create an instrumented Cohere client."""
    if not config.has_cohere():
        pytest.skip("COHERE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_cohere import CohereInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_cohere not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    CohereInstrumentor().instrument(tracer_provider=tracer_provider)

    import cohere

    client = cohere.ClientV2(api_key=config.cohere_api_key)

    yield client

    CohereInstrumentor().uninstrument()


@skip_if_no_cohere
class TestCohereChatCompletion:
    """Test Cohere chat completion instrumentation."""

    def test_basic_chat(self, cohere_client):
        """Test basic chat."""
        response = cohere_client.chat(
            model="command-r-plus",
            messages=[
                {"role": "user", "content": "Say 'Hello E2E Test' in exactly 3 words."}
            ],
        )

        assert response.message.content[0].text is not None
        time.sleep(2)
        print(f"Response: {response.message.content[0].text}")

    def test_streaming(self, cohere_client):
        """Test streaming chat."""
        chunks = []

        for event in cohere_client.chat_stream(
            model="command-r-plus",
            messages=[
                {"role": "user", "content": "Count from 1 to 5."}
            ],
        ):
            if hasattr(event, "text"):
                chunks.append(event.text)

        assert len(chunks) > 0
        print(f"Streamed: {''.join(chunks)}")

    def test_with_system_message(self, cohere_client):
        """Test with system message (preamble)."""
        response = cohere_client.chat(
            model="command-r-plus",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that responds briefly."},
                {"role": "user", "content": "What is 2+2?"},
            ],
        )

        assert response.message.content[0].text is not None

    def test_tool_calling(self, cohere_client):
        """Test tool/function calling."""
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

        response = cohere_client.chat(
            model="command-r-plus",
            messages=[
                {"role": "user", "content": "What's the weather in Paris?"}
            ],
            tools=tools,
        )

        if response.message.tool_calls:
            assert len(response.message.tool_calls) > 0
            print(f"Tool call: {response.message.tool_calls[0].function.name}")


@skip_if_no_cohere
class TestCohereErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, cohere_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            cohere_client.chat(
                model="invalid-model-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
