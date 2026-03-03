"""
E2E Tests for PydanticAI SDK Instrumentation

Tests PydanticAI instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_pydantic_ai():
    """Set up PydanticAI with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_pydantic_ai import PydanticAIInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_pydantic_ai not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    PydanticAIInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    PydanticAIInstrumentor().uninstrument()


@skip_if_no_google
class TestPydanticAIAgent:
    """Test PydanticAI agent instrumentation."""

    def test_simple_agent(self, setup_pydantic_ai):
        """Test simple agent run."""
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIModel

        model = OpenAIModel(
            config.google_model,
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = Agent(
            model,
            system_prompt="You are a helpful assistant. Answer briefly.",
        )

        result = agent.run_sync("What is 2+2?")

        assert result.data is not None
        assert "4" in str(result.data)
        time.sleep(2)
        print(f"Agent result: {result.data}")

    def test_agent_with_structured_output(self, setup_pydantic_ai):
        """Test agent with structured output."""
        from pydantic import BaseModel
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIModel

        class CityInfo(BaseModel):
            name: str
            country: str

        model = OpenAIModel(
            config.google_model,
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = Agent(
            model,
            result_type=CityInfo,
            system_prompt="Extract city information from the user query.",
        )

        result = agent.run_sync("Tell me about Paris, France")

        assert result.data is not None
        assert isinstance(result.data, CityInfo)
        assert "Paris" in result.data.name
        print(f"Structured output: {result.data}")

    def test_agent_with_tools(self, setup_pydantic_ai):
        """Test agent with tool use."""
        from pydantic_ai import Agent, RunContext
        from pydantic_ai.models.openai import OpenAIModel

        model = OpenAIModel(
            config.google_model,
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = Agent(
            model,
            system_prompt="You are a math assistant. Use tools to compute answers.",
        )

        @agent.tool_plain
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        result = agent.run_sync("What is 15 + 27?")

        assert result.data is not None
        assert "42" in str(result.data)
        print(f"Tool result: {result.data}")


@skip_if_no_google
class TestPydanticAIAsync:
    """Test async PydanticAI operations."""

    @pytest.mark.asyncio
    async def test_async_agent(self, setup_pydantic_ai):
        """Test async agent run."""
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIModel

        model = OpenAIModel(
            config.google_model,
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = Agent(model, system_prompt="Answer briefly.")

        result = await agent.run("Say hello in one word.")

        assert result.data is not None
