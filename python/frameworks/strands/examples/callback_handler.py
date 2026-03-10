"""Strands Agent with custom callback handler example.

This example demonstrates:
- Using StrandsCallbackHandler for extended tracing
- Capturing agent lifecycle events
- Custom event handling
"""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_strands import (
    configure_strands_tracing,
    StrandsCallbackHandler,
)

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="strands-callback-demo",
)

# Configure Strands to use TraceAI
configure_strands_tracing(tracer_provider=trace_provider)

# Import Strands (after configuration)
from strands import Agent


def main():
    # Create a custom callback handler with additional tracing
    callback = StrandsCallbackHandler(
        tracer_provider=trace_provider,
        capture_input=True,
        capture_output=True,
    )

    # Create an agent with the callback handler
    agent = Agent(
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        system_prompt="You are a helpful AI assistant.",
        callback_handler=callback,
        trace_attributes={
            "session.id": "callback-demo-001",
            "tags": ["demo", "callback"],
        },
    )

    print("=" * 60)
    print("Strands Callback Handler Demo")
    print("=" * 60)

    # Simulate agent lifecycle events manually
    # (In real usage, Strands calls these automatically)

    prompt = "Explain quantum computing in simple terms."
    print(f"\nUser: {prompt}")

    # These would be called by Strands internally
    callback.on_agent_start(agent, prompt)

    # Make the actual call
    response = agent(prompt)

    callback.on_agent_end(agent, response)

    print(f"Agent: {response}")

    print("\n" + "=" * 60)
    print("Callback events have been traced!")
    print("View the detailed spans in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
