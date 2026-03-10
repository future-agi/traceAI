"""
E2E Tests for Agno SDK Instrumentation

Tests Agno (OpenInference-based) instrumentation.
Agno agents can use OpenAI-compat LLMs; tries Google endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_agno():
    """Set up Agno with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_agno import AgnoInstrumentorWrapper
    except (ImportError, AttributeError):
        pytest.skip("traceai_agno not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    AgnoInstrumentorWrapper().instrument(tracer_provider=tracer_provider)

    yield

    AgnoInstrumentorWrapper().uninstrument()


@skip_if_no_google
class TestAgnoAgent:
    """Test Agno agent operations."""

    def test_simple_agent(self, setup_agno):
        """Test simple Agno agent."""
        from agno.agent import Agent
        from agno.models.openai import OpenAIChat

        model = OpenAIChat(
            id=config.google_model,
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = Agent(
            model=model,
            instructions=["You are a helpful assistant. Answer briefly."],
        )

        response = agent.run("What is 2+2?")

        assert response is not None
        assert response.content is not None
        assert "4" in response.content
        time.sleep(2)
        print(f"Agno result: {response.content}")

    def test_agent_with_tool(self, setup_agno):
        """Test Agno agent with tool."""
        from agno.agent import Agent
        from agno.models.openai import OpenAIChat
        from agno.tools import tool

        @tool
        def add(a: int, b: int) -> str:
            """Add two numbers."""
            return str(a + b)

        model = OpenAIChat(
            id=config.google_model,
            base_url=config.google_openai_base_url,
            api_key=config.google_api_key,
        )

        agent = Agent(
            model=model,
            tools=[add],
            instructions=["Use tools to compute answers."],
        )

        response = agent.run("What is 15 + 27?")

        assert response is not None
        assert "42" in str(response.content)
        print(f"Agno tool result: {response.content}")
