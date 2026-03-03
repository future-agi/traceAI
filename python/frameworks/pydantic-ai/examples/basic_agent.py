"""Basic Pydantic AI agent example with tracing.

This example demonstrates basic agent usage with OpenTelemetry tracing.
"""

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


def main():
    # Setup tracing
    provider = setup_tracing()

    # Initialize Pydantic AI instrumentation
    PydanticAIInstrumentor().instrument(tracer_provider=provider)

    # Import after instrumentation
    from pydantic_ai import Agent

    # Create a simple agent
    agent = Agent(
        'openai:gpt-4o-mini',
        instructions='You are a helpful assistant. Be concise.',
    )

    # Run the agent
    print("Running agent...")
    result = agent.run_sync('What is 2 + 2?')

    print(f"\nResult: {result.output}")
    print(f"Usage: {result.usage}")


if __name__ == "__main__":
    main()
