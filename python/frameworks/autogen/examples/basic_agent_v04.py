"""Basic AutoGen v0.4 (AgentChat) example with tracing.

This example demonstrates:
- Setting up instrumentation for AutoGen v0.4
- Creating AssistantAgent with OpenAI model
- Running a simple agent query with automatic tracing
"""

import asyncio
import os

# Setup tracing first
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Register and instrument
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-v04-basic",
)
instrument_autogen(tracer_provider=trace_provider)

# Now import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient


async def main():
    # Create model client
    model_client = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Create assistant agent
    agent = AssistantAgent(
        name="assistant",
        model_client=model_client,
        system_message="You are a helpful AI assistant. Be concise and clear.",
    )

    # Send a message and get response - automatically traced
    print("Sending message to agent...")

    response = await agent.on_messages(
        messages=[TextMessage(content="What is the capital of France?", source="user")],
        cancellation_token=None,
    )

    print(f"\nAgent response: {response.chat_message.content}")

    # Check for token usage
    if hasattr(response, "inner_messages") and response.inner_messages:
        for msg in response.inner_messages:
            if hasattr(msg, "models_usage") and msg.models_usage:
                print(f"Token usage: {msg.models_usage}")


if __name__ == "__main__":
    asyncio.run(main())
