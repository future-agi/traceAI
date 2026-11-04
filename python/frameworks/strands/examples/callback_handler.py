"""Strands Agent with custom callback handler example.

This example demonstrates:
- Using StrandsCallbackHandler for extended tracing
- Capturing agent lifecycle events
- Custom event handling
"""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import HTTPSpanExporter
from traceai_strands import StrandsSpanExporter
from traceai_strands import (
    StrandsCallbackHandler,
)

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="strands-callback-demo",
    # set_global_tracer_provider=True is required: Strands creates its spans from
    # OpenTelemetry's global tracer provider, so without it no Strands span reaches
    # this provider and nothing is sent to Future AGI.
    set_global_tracer_provider=True,
    span_exporter=StrandsSpanExporter(HTTPSpanExporter(), capture_content=True),
)

# Strands emits native spans; no additional telemetry exporter is needed.

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
        callback_handler=None,
        trace_attributes={
            "session.id": "callback-demo-001",
            "tags": ["demo", "callback"],
        },
    )

    print("=" * 60)
    print("Strands Callback Handler Demo")
    print("=" * 60)

    # Native Agent spans need no callback. The callback API is for separately
    # managed application operations; do not duplicate the Agent lifecycle.

    prompt = "Explain quantum computing in simple terms."
    print(f"\nUser: {prompt}")

    # Make the actual call
    response = agent(prompt)


    print(f"Agent: {response}")

    # A separate, application-owned operation using the legacy manual API.
    def format_answer(text: str) -> str:
        return text.strip()

    callback.on_tool_start(format_answer, {"text": str(response)})
    formatted = format_answer(str(response))
    callback.on_tool_end(format_answer, formatted)

    print("\n" + "=" * 60)
    print("The native agent spans have been exported without callback duplicates.")
    print("View the detailed spans in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    finally:
        trace_provider.force_flush()
        trace_provider.shutdown()
