"""
E2E Tests for Vertex AI SDK Instrumentation

Tests Vertex AI instrumentation. Requires GCP project and credentials.
"""

import pytest
import time

from config import config, skip_if_no_vertexai


@pytest.fixture(scope="module")
def vertexai_model():
    """Create an instrumented Vertex AI model."""
    if not config.has_vertexai():
        pytest.skip("Google Cloud not configured")

    from fi_instrumentation import register
    try:
        from traceai_vertexai import VertexAIInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_vertexai not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    VertexAIInstrumentor().instrument(tracer_provider=tracer_provider)

    import vertexai
    from vertexai.generative_models import GenerativeModel

    vertexai.init(project=config.google_cloud_project)
    model = GenerativeModel("gemini-2.0-flash")

    yield model

    VertexAIInstrumentor().uninstrument()


@skip_if_no_vertexai
class TestVertexAIGenerateContent:
    """Test Vertex AI generate_content instrumentation."""

    def test_basic_generate(self, vertexai_model):
        """Test basic content generation."""
        response = vertexai_model.generate_content(
            "Say 'Hello E2E Test' in exactly 3 words."
        )

        assert response.text is not None
        time.sleep(2)
        print(f"Response: {response.text}")

    def test_streaming(self, vertexai_model):
        """Test streaming content generation."""
        chunks = []

        response = vertexai_model.generate_content(
            "Count from 1 to 5.",
            stream=True,
        )

        for chunk in response:
            if chunk.text:
                chunks.append(chunk.text)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed response: {full_response}")

    def test_with_generation_config(self, vertexai_model):
        """Test with generation config."""
        from vertexai.generative_models import GenerationConfig

        response = vertexai_model.generate_content(
            "What is the capital of France?",
            generation_config=GenerationConfig(
                temperature=0.0,
                max_output_tokens=20,
            ),
        )

        assert response.text is not None
        assert "Paris" in response.text

    def test_multi_turn(self, vertexai_model):
        """Test multi-turn conversation via chat."""
        chat = vertexai_model.start_chat()

        response1 = chat.send_message("My name is Alice.")
        assert response1.text is not None

        response2 = chat.send_message("What's my name?")
        assert "Alice" in response2.text

    def test_tool_calling(self, vertexai_model):
        """Test function calling."""
        from vertexai.generative_models import FunctionDeclaration, Tool

        get_weather = FunctionDeclaration(
            name="get_weather",
            description="Get weather for a location",
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name",
                    }
                },
                "required": ["location"],
            },
        )

        tool = Tool(function_declarations=[get_weather])

        model_with_tools = vertexai_model.__class__(
            "gemini-2.0-flash",
            tools=[tool],
        )

        response = model_with_tools.generate_content(
            "What's the weather in Paris?"
        )

        has_function_call = False
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if part.function_call:
                    has_function_call = True
                    assert part.function_call.name == "get_weather"
                    print(f"Function call: {part.function_call.name}")
                    break

        if not has_function_call:
            print("Model chose not to use function (acceptable)")


@skip_if_no_vertexai
class TestVertexAIErrorHandling:
    """Test error handling."""

    def test_invalid_model(self):
        """Test handling of invalid model."""
        if not config.has_vertexai():
            pytest.skip("Google Cloud not configured")

        try:
            from vertexai.generative_models import GenerativeModel
        except ImportError:
            pytest.skip("vertexai not installed")

        model = GenerativeModel("invalid-model-xyz")

        with pytest.raises(Exception):
            model.generate_content("test")
