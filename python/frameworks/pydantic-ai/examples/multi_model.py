"""Pydantic AI multi-model example.

This example demonstrates using different model providers with tracing.
Model provider is automatically detected from model names.
"""

import asyncio

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_pydantic_ai import PydanticAIInstrumentor, get_model_provider


def setup_tracing():
    """Setup OpenTelemetry with console exporter for demo."""
    provider = TracerProvider()
    processor = SimpleSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider


async def query_agent(agent, prompt: str, model_name: str):
    """Query an agent and print results."""
    print(f"\n{'='*60}")
    print(f"Model: {model_name}")
    print(f"Provider: {get_model_provider(model_name)}")
    print(f"Prompt: {prompt}")
    print("-" * 60)

    result = await agent.run(prompt)

    print(f"Response: {result.output}")
    if result.usage:
        print(f"Tokens: {result.usage.total_tokens}")


async def main():
    # Setup tracing
    provider = setup_tracing()

    # Initialize Pydantic AI instrumentation
    PydanticAIInstrumentor().instrument(tracer_provider=provider)

    # Import after instrumentation
    from pydantic_ai import Agent

    # Define different models to test
    # Note: You'll need appropriate API keys set for each provider
    models = [
        'openai:gpt-4o-mini',
        # 'anthropic:claude-3-haiku-20240307',
        # 'google-gla:gemini-1.5-flash',
    ]

    prompt = "What is the capital of Japan? Answer in one sentence."

    for model_name in models:
        try:
            agent = Agent(
                model_name,
                instructions='Be concise and accurate.',
            )
            await query_agent(agent, prompt, model_name)
        except Exception as e:
            print(f"\n[Error with {model_name}]: {e}")

    print("\n" + "=" * 60)
    print("Multi-model comparison complete!")


if __name__ == "__main__":
    asyncio.run(main())
