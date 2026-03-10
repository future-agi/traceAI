"""
E2E Tests for OpenAI Agents SDK Instrumentation

Tests OpenAI Agents SDK instrumentation. Attempts Google endpoint first,
falls back to requiring OPENAI_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_google, skip_if_no_openai


@pytest.fixture(scope="module")
def setup_openai_agents():
    """Set up OpenAI Agents with instrumentation."""
    if not config.has_google() and not config.has_openai():
        pytest.skip("GOOGLE_API_KEY or OPENAI_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_openai_agents import OpenAIAgentsInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_openai_agents not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    try:
        OpenAIAgentsInstrumentor().instrument(tracer_provider=tracer_provider)
    except (ImportError, AttributeError) as e:
        pytest.skip(f"traceai_openai_agents incompatible: {e}")

    yield

    try:
        OpenAIAgentsInstrumentor().uninstrument()
    except Exception:
        pass


@pytest.mark.skipif(
    not (config.has_google() or config.has_openai()),
    reason="GOOGLE_API_KEY or OPENAI_API_KEY required"
)
class TestOpenAIAgentsBasic:
    """Test OpenAI Agents SDK basic operations."""

    def test_simple_agent(self, setup_openai_agents):
        """Test simple agent run."""
        from agents import Agent, Runner

        # Use OpenAI if available, otherwise try Google
        if config.has_openai():
            model = "gpt-4o-mini"
        else:
            model = config.google_model

        agent = Agent(
            name="Assistant",
            instructions="You are a helpful assistant. Answer briefly.",
            model=model,
        )

        result = Runner.run_sync(agent, "What is 2+2?")

        assert result.final_output is not None
        assert "4" in str(result.final_output)
        time.sleep(2)
        print(f"Agent result: {result.final_output}")

    def test_agent_with_tools(self, setup_openai_agents):
        """Test agent with function tools."""
        from agents import Agent, Runner, function_tool

        @function_tool
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        if config.has_openai():
            model = "gpt-4o-mini"
        else:
            model = config.google_model

        agent = Agent(
            name="Calculator",
            instructions="You are a calculator. Use tools to compute.",
            model=model,
            tools=[add],
        )

        result = Runner.run_sync(agent, "What is 15 + 27?")

        assert result.final_output is not None
        assert "42" in str(result.final_output)
        print(f"Tool result: {result.final_output}")

    def test_agent_handoff(self, setup_openai_agents):
        """Test agent handoff between agents."""
        from agents import Agent, Runner

        if config.has_openai():
            model = "gpt-4o-mini"
        else:
            model = config.google_model

        math_agent = Agent(
            name="Math Expert",
            instructions="You are a math expert. Answer math questions briefly.",
            model=model,
        )

        triage_agent = Agent(
            name="Triage",
            instructions="You route questions to specialists. For math, hand off to the math expert.",
            model=model,
            handoffs=[math_agent],
        )

        result = Runner.run_sync(triage_agent, "What is 5 * 6?")

        assert result.final_output is not None
        print(f"Handoff result: {result.final_output}")
