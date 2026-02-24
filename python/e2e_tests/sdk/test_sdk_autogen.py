"""
E2E Tests for AutoGen SDK Instrumentation

Tests AutoGen instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_autogen():
    """Set up AutoGen with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

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


@pytest.fixture
def oai_config():
    """AutoGen OAI config pointing to Google endpoint."""
    return {
        "config_list": [
            {
                "model": config.google_model,
                "base_url": config.google_openai_base_url,
                "api_key": config.google_api_key,
            }
        ],
        "temperature": 0,
    }


@skip_if_no_google
class TestAutoGenConversation:
    """Test AutoGen conversation instrumentation."""

    def test_simple_agent_reply(self, setup_autogen, oai_config):
        """Test basic assistant agent reply."""
        from autogen import AssistantAgent, UserProxyAgent

        assistant = AssistantAgent(
            name="assistant",
            llm_config=oai_config,
            system_message="You are a helpful assistant. Answer briefly.",
        )

        user_proxy = UserProxyAgent(
            name="user",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=1,
            code_execution_config=False,
        )

        user_proxy.initiate_chat(
            assistant,
            message="What is 2+2? Answer with just the number.",
        )

        # Get last message from assistant
        messages = user_proxy.chat_messages[assistant]
        assert len(messages) >= 2
        last_reply = messages[-1]["content"]
        assert "4" in last_reply

        time.sleep(2)
        print(f"AutoGen reply: {last_reply}")

    def test_two_agent_conversation(self, setup_autogen, oai_config):
        """Test two-agent conversation."""
        from autogen import AssistantAgent, UserProxyAgent

        assistant = AssistantAgent(
            name="math_expert",
            llm_config=oai_config,
            system_message="You are a math expert. Answer math questions briefly.",
        )

        user_proxy = UserProxyAgent(
            name="student",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=2,
            code_execution_config=False,
        )

        user_proxy.initiate_chat(
            assistant,
            message="What is the capital of France? Answer in one word.",
        )

        messages = user_proxy.chat_messages[assistant]
        assert len(messages) >= 2
        print(f"Conversation length: {len(messages)} messages")


@skip_if_no_google
class TestAutoGenErrorHandling:
    """Test error handling."""

    def test_invalid_config(self, setup_autogen):
        """Test handling of invalid LLM config."""
        from autogen import AssistantAgent, UserProxyAgent

        bad_config = {
            "config_list": [
                {
                    "model": "invalid-model-xyz",
                    "base_url": config.google_openai_base_url,
                    "api_key": config.google_api_key,
                }
            ],
        }

        assistant = AssistantAgent(
            name="bad_assistant",
            llm_config=bad_config,
        )

        user_proxy = UserProxyAgent(
            name="user",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=0,
            code_execution_config=False,
        )

        with pytest.raises(Exception):
            user_proxy.initiate_chat(
                assistant,
                message="test",
            )
