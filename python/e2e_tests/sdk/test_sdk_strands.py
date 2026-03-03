"""
E2E Tests for AWS Strands SDK Instrumentation

Tests Strands agents instrumentation. Requires AWS credentials for Bedrock models.
"""

import pytest
import time

from config import config, skip_if_no_bedrock


@pytest.fixture(scope="module")
def setup_strands():
    """Set up Strands with instrumentation."""
    if not config.has_bedrock():
        pytest.skip("AWS credentials not set")

    from fi_instrumentation import register
    try:
        from traceai_strands import StrandsInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_strands not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    StrandsInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    StrandsInstrumentor().uninstrument()


@skip_if_no_bedrock
class TestStrandsAgent:
    """Test Strands agent operations."""

    def test_simple_agent(self, setup_strands):
        """Test simple Strands agent."""
        from strands import Agent

        agent = Agent(
            model="anthropic.claude-3-haiku-20240307-v1:0",
            system_prompt="You are a helpful assistant. Answer briefly.",
            region=config.aws_bedrock_region,
        )

        response = agent("What is 2+2?")

        assert response is not None
        assert "4" in str(response)
        time.sleep(2)
        print(f"Strands result: {response}")

    def test_agent_with_tool(self, setup_strands):
        """Test Strands agent with tool."""
        from strands import Agent, tool

        @tool
        def get_weather(location: str) -> str:
            """Get weather for a location."""
            return f"Sunny and 72F in {location}"

        agent = Agent(
            model="anthropic.claude-3-haiku-20240307-v1:0",
            system_prompt="You are a weather assistant. Use tools.",
            tools=[get_weather],
            region=config.aws_bedrock_region,
        )

        response = agent("What's the weather in Paris?")

        assert response is not None
        assert "Paris" in str(response) or "72" in str(response)
        print(f"Strands tool result: {response}")
