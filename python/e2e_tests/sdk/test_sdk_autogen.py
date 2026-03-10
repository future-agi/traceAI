"""
E2E Tests for AutoGen v0.4+ SDK Instrumentation

Tests AutoGen AgentChat instrumentation using Google's Gemini model via litellm.
"""

import os
import pytest
import asyncio
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_autogen():
    """Set up AutoGen with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    os.environ.setdefault("GEMINI_API_KEY", config.google_api_key)

    from fi_instrumentation import register
    try:
        from traceai_autogen import AutogenInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_autogen not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    AutogenInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    AutogenInstrumentor().uninstrument()


@skip_if_no_google
class TestAutoGenAgentChat:
    """Test AutoGen v0.4 AgentChat instrumentation."""

    def test_assistant_agent_run(self, setup_autogen):
        """Test basic AssistantAgent with team run."""
        from autogen_agentchat.agents import AssistantAgent
        from autogen_agentchat.teams import RoundRobinGroupChat
        from autogen_agentchat.conditions import TextMentionTermination
        from autogen_ext.models.openai import OpenAIChatCompletionClient

        model_client = OpenAIChatCompletionClient(
            model="gemini-2.0-flash",
            api_key=config.google_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        agent = AssistantAgent(
            name="assistant",
            model_client=model_client,
            system_message="You are a helpful assistant. Answer briefly. Say TERMINATE when done.",
        )

        termination = TextMentionTermination("TERMINATE")
        team = RoundRobinGroupChat(
            participants=[agent],
            termination_condition=termination,
        )

        result = asyncio.run(team.run(task="What is 2+2? Answer with just the number."))

        assert result is not None
        assert hasattr(result, "messages")
        assert len(result.messages) >= 1
        time.sleep(2)
        print(f"AutoGen v0.4 result: {result.messages[-1].content if result.messages else 'no messages'}")
