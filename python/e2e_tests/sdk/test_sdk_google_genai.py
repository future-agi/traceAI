"""
E2E Tests for Google GenAI SDK Instrumentation

Tests Google GenAI native SDK (google.genai.Client) with tracing.
Uses GOOGLE_API_KEY directly — no OpenAI-compat wrapper needed.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def genai_client():
    """Create an instrumented Google GenAI client."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_google_genai import GoogleGenAIInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_google_genai not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    GoogleGenAIInstrumentor().instrument(tracer_provider=tracer_provider)

    from google import genai

    client = genai.Client(api_key=config.google_api_key)

    yield client

    GoogleGenAIInstrumentor().uninstrument()


@pytest.fixture(scope="module")
def async_genai_client():
    """Return the same client — google.genai supports async via aio namespace."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from google import genai

    return genai.Client(api_key=config.google_api_key)


@skip_if_no_google
class TestGoogleGenAIGenerateContent:
    """Test Google GenAI generate_content instrumentation."""

    def test_basic_generate(self, genai_client):
        """Test basic content generation."""
        response = genai_client.models.generate_content(
            model=config.google_model,
            contents="Say 'Hello E2E Test' in exactly 3 words.",
        )

        assert response.text is not None
        assert len(response.text) > 0

        time.sleep(2)
        print(f"Response: {response.text}")

    def test_with_system_instruction(self, genai_client):
        """Test generation with system instruction."""
        from google.genai import types

        response = genai_client.models.generate_content(
            model=config.google_model,
            contents="What is 2+2?",
            config=types.GenerateContentConfig(
                system_instruction="You are a helpful assistant that responds briefly.",
                max_output_tokens=50,
            ),
        )

        assert response.text is not None
        assert "4" in response.text

    def test_multi_turn_conversation(self, genai_client):
        """Test multi-turn conversation with content list."""
        from google.genai import types

        contents = [
            types.Content(
                role="user",
                parts=[types.Part(text="My name is Alice.")],
            ),
            types.Content(
                role="model",
                parts=[types.Part(text="Nice to meet you, Alice!")],
            ),
            types.Content(
                role="user",
                parts=[types.Part(text="What's my name?")],
            ),
        ]

        response = genai_client.models.generate_content(
            model=config.google_model,
            contents=contents,
        )

        assert "Alice" in response.text

    def test_streaming(self, genai_client):
        """Test streaming content generation."""
        chunks = []

        for chunk in genai_client.models.generate_content_stream(
            model=config.google_model,
            contents="Count from 1 to 5.",
        ):
            if chunk.text:
                chunks.append(chunk.text)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_with_generation_config(self, genai_client):
        """Test with explicit generation config."""
        from google.genai import types

        response = genai_client.models.generate_content(
            model=config.google_model,
            contents="What is the capital of France?",
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=20,
            ),
        )

        assert response.text is not None
        assert "Paris" in response.text


@skip_if_no_google
class TestGoogleGenAIToolUse:
    """Test Google GenAI function calling / tool use."""

    def test_function_calling(self, genai_client):
        """Test function calling with Google GenAI."""
        from google.genai import types

        get_weather = types.FunctionDeclaration(
            name="get_weather",
            description="Get the current weather for a location",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "location": types.Schema(
                        type=types.Type.STRING,
                        description="City name",
                    ),
                },
                required=["location"],
            ),
        )

        tool = types.Tool(function_declarations=[get_weather])

        response = genai_client.models.generate_content(
            model=config.google_model,
            contents="What's the weather in Paris?",
            config=types.GenerateContentConfig(
                tools=[tool],
            ),
        )

        # Check if function call was made
        has_function_call = False
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if part.function_call:
                    has_function_call = True
                    assert part.function_call.name == "get_weather"
                    print(f"Function call: {part.function_call.name}")
                    print(f"Args: {part.function_call.args}")
                    break

        if not has_function_call:
            print("Model chose not to use function (acceptable)")


@skip_if_no_google
class TestGoogleGenAIAsync:
    """Test async Google GenAI operations."""

    @pytest.mark.asyncio
    async def test_async_generate(self, async_genai_client):
        """Test async content generation."""
        response = await async_genai_client.aio.models.generate_content(
            model=config.google_model,
            contents="Say 'async test' briefly.",
        )

        assert response.text is not None

    @pytest.mark.asyncio
    async def test_async_streaming(self, async_genai_client):
        """Test async streaming."""
        chunks = []

        async for chunk in async_genai_client.aio.models.generate_content_stream(
            model=config.google_model,
            contents="Count 1 to 3.",
        ):
            if chunk.text:
                chunks.append(chunk.text)

        assert len(chunks) > 0


@skip_if_no_google
class TestGoogleGenAIErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, genai_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            genai_client.models.generate_content(
                model="invalid-model-xyz",
                contents="test",
            )
