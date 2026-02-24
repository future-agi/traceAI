"""Pydantic AI agent with tools example.

This example demonstrates tool usage with OpenTelemetry tracing.
Tools are automatically traced when instrumentation is enabled.
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
    from pydantic_ai import Agent, RunContext

    # Create agent
    agent = Agent(
        'openai:gpt-4o-mini',
        instructions='You are a helpful assistant with access to tools.',
    )

    # Define tools
    @agent.tool
    def get_weather(ctx: RunContext, city: str) -> str:
        """Get the current weather for a city.

        Args:
            city: The city name to get weather for.

        Returns:
            Weather information string.
        """
        # In real usage, this would call a weather API
        weather_data = {
            "San Francisco": "Sunny, 68°F (20°C)",
            "New York": "Cloudy, 55°F (13°C)",
            "London": "Rainy, 48°F (9°C)",
            "Tokyo": "Clear, 72°F (22°C)",
        }
        return weather_data.get(city, f"Weather data not available for {city}")

    @agent.tool
    def calculate(ctx: RunContext, expression: str) -> str:
        """Evaluate a mathematical expression.

        Args:
            expression: A mathematical expression to evaluate.

        Returns:
            The result of the calculation.
        """
        try:
            # Simple and safe eval for demo (use proper math parser in production)
            result = eval(expression, {"__builtins__": {}}, {})
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    # Run agent with a query that uses tools
    print("Running agent with tools...")
    result = agent.run_sync(
        "What's the weather like in San Francisco? "
        "Also, what is 15 * 23?"
    )

    print(f"\nResult: {result.output}")
    print(f"Usage: {result.usage}")


if __name__ == "__main__":
    main()
