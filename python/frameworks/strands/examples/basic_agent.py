"""Basic Strands Agent example with TraceAI observability.

This example demonstrates:
- Setting up TraceAI with Strands Agents
- Creating a simple agent with tracing
- Making agent calls with automatic trace export
"""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_strands import configure_strands_tracing

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="strands-basic-agent",
)

# Configure Strands to use TraceAI
configure_strands_tracing(tracer_provider=trace_provider)

# Import Strands (after configuration)
from strands import Agent


def main():
    # Create a simple agent
    agent = Agent(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",  # Bedrock Claude
        system_prompt="You are a helpful AI assistant that provides concise answers.",
        trace_attributes={
            "session.id": "demo-session-001",
            "user.id": "demo@example.com",
            "tags": ["demo", "basic-agent"],
        },
    )

    # Have a conversation
    questions = [
        "What is the capital of France?",
        "What's the population there?",
        "What's a famous landmark?",
    ]

    print("=" * 60)
    print("Basic Strands Agent Demo with TraceAI")
    print("=" * 60)

    for question in questions:
        print(f"\nUser: {question}")
        response = agent(question)
        print(f"Agent: {response}")

    print("\n" + "=" * 60)
    print("Traces have been sent to TraceAI!")
    print("View them in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
