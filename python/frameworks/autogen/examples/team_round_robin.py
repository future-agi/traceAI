"""AutoGen v0.4 RoundRobinGroupChat example with tracing.

This example demonstrates:
- Creating multiple specialized agents
- Orchestrating them with RoundRobinGroupChat
- Tracing team interactions and message flow
"""

import asyncio
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-team-roundrobin",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Create specialized agents for a code review team
    coder = AssistantAgent(
        name="coder",
        model_client=model,
        system_message="""You are an expert Python developer.
        Write clean, efficient, and well-documented code.
        Follow PEP 8 style guidelines.""",
    )

    reviewer = AssistantAgent(
        name="reviewer",
        model_client=model,
        system_message="""You are a senior code reviewer.
        Review code for:
        - Correctness and edge cases
        - Performance optimizations
        - Code style and readability
        - Security vulnerabilities
        Provide constructive feedback.""",
    )

    tester = AssistantAgent(
        name="tester",
        model_client=model,
        system_message="""You are a QA engineer specializing in Python testing.
        Write comprehensive unit tests using pytest.
        Cover edge cases and ensure good test coverage.
        Say APPROVED when tests are complete and passing.""",
    )

    # Create team with round-robin communication
    team = RoundRobinGroupChat(
        participants=[coder, reviewer, tester],
        termination_condition=MaxMessageTermination(max_messages=8)
        | TextMentionTermination("APPROVED"),
    )

    # Run the team task - all interactions are traced
    print("Starting code review team...")
    print("-" * 50)

    result = await team.run(
        task="Write a Python function to validate email addresses using regex. "
        "Include proper error handling and documentation."
    )

    print("-" * 50)
    print("\n--- Team Task Complete ---")
    print(f"Total messages: {len(result.messages)}")
    print(f"Stop reason: {result.stop_reason}")

    # Print conversation summary
    print("\n--- Conversation Flow ---")
    for i, msg in enumerate(result.messages):
        source = getattr(msg, "source", "unknown")
        content = getattr(msg, "content", str(msg))
        preview = content[:100] + "..." if len(content) > 100 else content
        print(f"{i + 1}. [{source}]: {preview}")


if __name__ == "__main__":
    asyncio.run(main())
