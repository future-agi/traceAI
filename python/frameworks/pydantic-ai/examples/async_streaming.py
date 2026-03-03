"""Pydantic AI async streaming example.

This example demonstrates async streaming with OpenTelemetry tracing.
Streaming spans track chunk counts and total duration.
"""

import asyncio

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


async def main():
    # Setup tracing
    provider = setup_tracing()

    # Initialize Pydantic AI instrumentation
    PydanticAIInstrumentor().instrument(tracer_provider=provider)

    # Import after instrumentation
    from pydantic_ai import Agent

    # Create agent
    agent = Agent(
        'openai:gpt-4o-mini',
        instructions='You are a creative writer. Write engaging content.',
    )

    # Streaming example
    print("Streaming response:\n")
    print("-" * 50)

    async with agent.run_stream("Write a haiku about coding") as stream:
        async for chunk in stream.stream_text():
            print(chunk, end='', flush=True)

    print("\n" + "-" * 50)
    print("\nStreaming complete!")


if __name__ == "__main__":
    asyncio.run(main())
