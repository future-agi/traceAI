"""
E2E Tests for Claude Agent SDK Instrumentation

Tests Claude Agent SDK instrumentation. Requires ANTHROPIC_API_KEY and Claude CLI.
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
    async def test_simple_query(self, setup_claude_agent):
        """Test simple Claude agent query using the query() function."""
        from claude_agent_sdk import query, ClaudeAgentOptions

        messages = []
        async for msg in query(
            prompt="What is 2+2? Answer with just the number.",
            options=ClaudeAgentOptions(
                max_turns=1,
            ),
        ):
            messages.append(msg)

        assert len(messages) >= 1

        # Check that we got a ResultMessage
        result_msg = [m for m in messages if type(m).__name__ == "ResultMessage"]
        assert len(result_msg) >= 1

        time.sleep(2)
        print(f"Claude Agent query result: {len(messages)} messages received")

    @pytest.mark.asyncio
    async def test_sdk_client(self, setup_claude_agent):
        """Test Claude agent using ClaudeSDKClient (instrumented path)."""
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

        options = ClaudeAgentOptions(
            max_turns=1,
        )

        async with ClaudeSDKClient(options=options) as client:
            await client.query("What is the capital of France? Answer in one word.")

            messages = []
            async for msg in client.receive_response():
                messages.append(msg)

        assert len(messages) >= 1

        result_msg = [m for m in messages if type(m).__name__ == "ResultMessage"]
        assert len(result_msg) >= 1

        time.sleep(2)
        print(f"Claude SDK Client result: {len(messages)} messages received")
