"""AutoGen v0.4 streaming team example with tracing.

This example demonstrates:
- Using run_stream for streaming responses
- Processing messages as they arrive
- Tracing streaming interactions
"""

import asyncio
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-streaming",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Create a creative writing team
    writer = AssistantAgent(
        name="writer",
        model_client=model,
        system_message="""You are a creative writer.
        Write engaging, vivid prose with strong imagery.
        Focus on showing rather than telling.""",
    )

    editor = AssistantAgent(
        name="editor",
        model_client=model,
        system_message="""You are a skilled editor.
        Review writing for:
        - Grammar and style
        - Pacing and flow
        - Word choice improvements
        Provide specific, actionable feedback.""",
    )

    # Create team
    team = RoundRobinGroupChat(
        participants=[writer, editor],
        termination_condition=MaxMessageTermination(max_messages=4),
    )

    # Run with streaming - messages are traced as they arrive
    print("Starting creative writing session (streaming)...")
    print("-" * 50)

    message_count = 0
    async for message in team.run_stream(
        task="Write a short opening paragraph for a mystery novel set in a foggy London street."
    ):
        message_count += 1
        source = getattr(message, "source", "system")
        content = getattr(message, "content", str(message))

        # Check if it's a task result
        if hasattr(message, "messages"):
            print(f"\n[Stream complete - {len(message.messages)} messages total]")
            continue

        print(f"\n[{message_count}] {source}:")
        print("-" * 30)
        # Print content in chunks for streaming effect
        print(content)

    print("-" * 50)
    print(f"\nTotal streamed messages: {message_count}")


if __name__ == "__main__":
    asyncio.run(main())
