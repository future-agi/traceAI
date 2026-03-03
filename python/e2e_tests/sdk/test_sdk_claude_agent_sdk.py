"""
E2E Tests for Claude Agent SDK Instrumentation

Tests Claude Agent SDK instrumentation. Requires ANTHROPIC_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_anthropic


@pytest.fixture(scope="module")
def setup_claude_agent():
    """Set up Claude Agent SDK with instrumentation."""
    if not config.has_anthropic():
        pytest.skip("ANTHROPIC_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_claude_agent_sdk import ClaudeAgentInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_claude_agent_sdk not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    ClaudeAgentInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    ClaudeAgentInstrumentor().uninstrument()


@skip_if_no_anthropic
class TestClaudeAgentBasic:
    """Test Claude Agent SDK basic operations."""

    @pytest.mark.asyncio
    async def test_simple_agent(self, setup_claude_agent):
        """Test simple Claude agent run."""
        from claude_code_sdk import Agent

        agent = Agent(
            model="claude-3-5-haiku-latest",
            api_key=config.anthropic_api_key,
        )

        result = await agent.run("What is 2+2? Answer with just the number.")

        assert result is not None
        assert "4" in str(result)
        time.sleep(2)
        print(f"Claude Agent result: {result}")

    @pytest.mark.asyncio
    async def test_agent_with_tools(self, setup_claude_agent):
        """Test Claude agent with tool use."""
        from claude_code_sdk import Agent

        tools = [
            {
                "name": "get_weather",
                "description": "Get weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"}
                    },
                    "required": ["location"],
                },
            }
        ]

        agent = Agent(
            model="claude-3-5-haiku-latest",
            api_key=config.anthropic_api_key,
            tools=tools,
        )

        result = await agent.run("What's the weather in Tokyo?")

        assert result is not None
        print(f"Claude Agent tool result: {result}")
