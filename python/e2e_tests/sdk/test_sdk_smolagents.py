"""
E2E Tests for Smolagents SDK Instrumentation

Tests Smolagents (HuggingFace) instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_smolagents():
    """Set up Smolagents with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_smolagents import SmolagentsInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_smolagents not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    SmolagentsInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    SmolagentsInstrumentor().uninstrument()


@skip_if_no_google
class TestSmolagentsBasic:
    """Test Smolagents basic agent operations."""

    def test_simple_agent(self, setup_smolagents):
        """Test simple agent run."""
        from smolagents import CodeAgent, LiteLLMModel

        model = LiteLLMModel(
            model_id=f"openai/{config.google_model}",
            api_base=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = CodeAgent(
            tools=[],
            model=model,
            max_steps=2,
        )

        result = agent.run("What is 2+2? Answer with just the number.")

        assert result is not None
        assert "4" in str(result)
        time.sleep(2)
        print(f"Agent result: {result}")

    def test_agent_with_tool(self, setup_smolagents):
        """Test agent with custom tool."""
        from smolagents import CodeAgent, LiteLLMModel, tool

        @tool
        def add_numbers(a: int, b: int) -> int:
            """Add two numbers together.

            Args:
                a: First number
                b: Second number
            """
            return a + b

        model = LiteLLMModel(
            model_id=f"openai/{config.google_model}",
            api_base=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = CodeAgent(
            tools=[add_numbers],
            model=model,
            max_steps=3,
        )

        result = agent.run("What is 15 + 27?")

        assert result is not None
        assert "42" in str(result)
        print(f"Tool result: {result}")
