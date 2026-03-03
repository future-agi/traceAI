"""DeepSeek Reasoner (R1) model example with tracing."""
import os
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_deepseek import DeepSeekInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument DeepSeek
DeepSeekInstrumentor().instrument(tracer_provider=provider)


def main():
    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY", "your-api-key"),
        base_url="https://api.deepseek.com/v1",
    )

    # Math reasoning
    print("=== Math Problem with Reasoning ===")
    response = client.chat.completions.create(
        model="deepseek-reasoner",
        messages=[
            {
                "role": "user",
                "content": "A store sells apples for $2 each and oranges for $3 each. "
                "If someone buys 5 apples and some oranges for a total of $22, "
                "how many oranges did they buy?",
            }
        ],
    )

    message = response.choices[0].message
    if hasattr(message, "reasoning_content") and message.reasoning_content:
        print("Reasoning Process:")
        print("-" * 40)
        print(message.reasoning_content)
        print("-" * 40)
    print(f"\nFinal Answer: {message.content}")

    # Show token usage
    print(f"\nToken Usage:")
    print(f"  Prompt tokens: {response.usage.prompt_tokens}")
    print(f"  Completion tokens: {response.usage.completion_tokens}")
    if hasattr(response.usage, "completion_tokens_details"):
        details = response.usage.completion_tokens_details
        if hasattr(details, "reasoning_tokens") and details.reasoning_tokens:
            print(f"  Reasoning tokens: {details.reasoning_tokens}")

    # Logic puzzle
    print("\n" + "=" * 50)
    print("=== Logic Puzzle with Reasoning ===")
    response = client.chat.completions.create(
        model="deepseek-reasoner",
        messages=[
            {
                "role": "user",
                "content": "Three friends - Alice, Bob, and Carol - each have a different "
                "favorite color: red, blue, or green. We know that:\n"
                "1. Alice doesn't like red.\n"
                "2. Bob doesn't like blue or green.\n"
                "3. Carol likes green.\n"
                "What is each person's favorite color?",
            }
        ],
    )

    message = response.choices[0].message
    if hasattr(message, "reasoning_content") and message.reasoning_content:
        print("Reasoning Process:")
        print("-" * 40)
        print(message.reasoning_content)
        print("-" * 40)
    print(f"\nFinal Answer: {message.content}")

    # Code analysis
    print("\n" + "=" * 50)
    print("=== Code Analysis with Reasoning ===")
    code = """
def mystery(n):
    if n <= 1:
        return n
    return mystery(n-1) + mystery(n-2)
"""
    response = client.chat.completions.create(
        model="deepseek-reasoner",
        messages=[
            {
                "role": "user",
                "content": f"Analyze this code and explain what it does. "
                f"What would mystery(6) return?\n\n```python{code}```",
            }
        ],
    )

    message = response.choices[0].message
    if hasattr(message, "reasoning_content") and message.reasoning_content:
        print("Reasoning Process:")
        print("-" * 40)
        print(message.reasoning_content)
        print("-" * 40)
    print(f"\nFinal Answer: {message.content}")


if __name__ == "__main__":
    main()
