"""Conversation with memory example.

This example demonstrates maintaining conversation history
with OpenTelemetry tracing for each turn.
"""

import asyncio
from typing import List, Dict

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_pydantic_ai import PydanticAIInstrumentor


def setup_tracing():
    """Setup OpenTelemetry with console exporter for demo."""
    provider = TracerProvider()
    processor = SimpleSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider


class ConversationManager:
    """Manage conversation history and agent interactions."""

    def __init__(self, agent):
        self.agent = agent
        self.history: List[Dict] = []

    async def send_message(self, user_message: str) -> str:
        """Send a message and get response, maintaining history."""
        # Run agent with conversation history
        result = await self.agent.run(
            user_message,
            message_history=self.history if self.history else None,
        )

        # Update history
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": str(result.output)})

        return result.output

    def clear_history(self):
        """Clear conversation history."""
        self.history = []


async def main():
    # Setup tracing
    provider = setup_tracing()

    # Initialize Pydantic AI instrumentation
    PydanticAIInstrumentor().instrument(tracer_provider=provider)

    # Import after instrumentation
    from pydantic_ai import Agent

    # Create agent
    agent = Agent(
        "openai:gpt-4o-mini",
        instructions=(
            "You are a friendly assistant. Remember details from our conversation. "
            "Keep responses concise."
        ),
    )

    # Create conversation manager
    conversation = ConversationManager(agent)

    # Simulate multi-turn conversation
    print("Starting conversation...\n")
    print("-" * 50)

    exchanges = [
        "Hi! My name is Alice.",
        "I'm working on a Python project about machine learning.",
        "What's my name and what am I working on?",
    ]

    for user_msg in exchanges:
        print(f"User: {user_msg}")
        response = await conversation.send_message(user_msg)
        print(f"Assistant: {response}")
        print("-" * 50)

    print(f"\nConversation turns: {len(conversation.history) // 2}")


if __name__ == "__main__":
    asyncio.run(main())
