"""Error handling example with Pydantic AI.

This example demonstrates proper error handling and how errors
are captured in OpenTelemetry spans.
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
    from pydantic_ai import Agent, RunContext

    # Create agent
    agent = Agent(
        "openai:gpt-4o-mini",
        instructions="You are a helpful assistant.",
    )

    # Tool that may fail
    @agent.tool
    def divide_numbers(ctx: RunContext, a: float, b: float) -> float:
        """Divide two numbers.

        Args:
            a: Numerator
            b: Denominator

        Returns:
            Result of a / b
        """
        if b == 0:
            raise ValueError("Cannot divide by zero!")
        return a / b

    # Example 1: Successful operation
    print("Example 1: Successful division")
    print("-" * 50)
    try:
        result = await agent.run("What is 10 divided by 2? Use the divide tool.")
        print(f"Result: {result.output}")
    except Exception as e:
        print(f"Error: {e}")

    print()

    # Example 2: Operation that triggers tool error
    print("Example 2: Division by zero (tool error)")
    print("-" * 50)
    try:
        result = await agent.run("What is 10 divided by 0? Use the divide tool.")
        print(f"Result: {result.output}")
    except Exception as e:
        print(f"Caught error: {type(e).__name__}: {e}")
        print("(This error was captured in the span)")

    print()

    # Example 3: Retry with fallback
    print("Example 3: Retry pattern")
    print("-" * 50)

    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            result = await agent.run("What is 100 divided by 5? Use the divide tool.")
            print(f"Success on attempt {retry_count + 1}: {result.output}")
            break
        except Exception as e:
            retry_count += 1
            print(f"Attempt {retry_count} failed: {e}")
            if retry_count >= max_retries:
                print("Max retries exceeded")

    print("\nAll examples complete. Check span output for error attributes.")


if __name__ == "__main__":
    asyncio.run(main())
