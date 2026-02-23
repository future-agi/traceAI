"""Cerebras streaming example with TraceAI instrumentation."""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_cerebras import CerebrasInstrumentor

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="cerebras-streaming-example",
)

# Instrument Cerebras
CerebrasInstrumentor().instrument(tracer_provider=trace_provider)

# Now import and use Cerebras
from cerebras.cloud.sdk import Cerebras


def main():
    """Run a streaming chat completion example."""
    client = Cerebras()

    print("Cerebras Streaming Example")
    print("-" * 40)
    print()

    # Streaming chat completion
    stream = client.chat.completions.create(
        model="llama3.1-8b",
        messages=[
            {"role": "user", "content": "Write a haiku about artificial intelligence."},
        ],
        max_tokens=100,
        stream=True,
    )

    print("Response: ", end="")
    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)

        # Last chunk may contain usage info
        if chunk.usage:
            print()
            print()
            print(f"Total tokens: {chunk.usage.total_tokens}")

        if chunk.time_info:
            print(f"Total time: {chunk.time_info.total_time:.4f}s")

    print()


if __name__ == "__main__":
    main()
