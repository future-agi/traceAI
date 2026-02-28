"""Pydantic AI structured output example.

This example demonstrates using structured outputs with Pydantic models.
The result type is traced as part of the span attributes.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from pydantic import BaseModel, Field

from traceai_pydantic_ai import PydanticAIInstrumentor


class MovieRecommendation(BaseModel):
    """A movie recommendation."""

    title: str = Field(description="The movie title")
    year: int = Field(description="Release year")
    genre: str = Field(description="Primary genre")
    director: str = Field(description="Director name")
    rating: float = Field(description="Rating out of 10", ge=0, le=10)
    summary: str = Field(description="Brief plot summary")
    why_recommended: str = Field(description="Why this movie is recommended")


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

    # Create agent with structured output
    agent = Agent(
        'openai:gpt-4o-mini',
        result_type=MovieRecommendation,
        instructions=(
            'You are a movie recommendation expert. '
            'Provide thoughtful recommendations based on user preferences.'
        ),
    )

    # Run agent
    print("Getting movie recommendation...")
    result = agent.run_sync(
        "Recommend a sci-fi movie from the 2010s that deals with AI themes"
    )

    # Access structured output
    movie = result.output
    print(f"\nRecommendation: {movie.title} ({movie.year})")
    print(f"Genre: {movie.genre}")
    print(f"Director: {movie.director}")
    print(f"Rating: {movie.rating}/10")
    print(f"\nSummary: {movie.summary}")
    print(f"\nWhy recommended: {movie.why_recommended}")

    print(f"\nUsage: {result.usage}")


if __name__ == "__main__":
    main()
