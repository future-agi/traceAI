"""
Basic MiniMax chat completion example with traceAI instrumentation.
"""

import os

from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_minimax import MiniMaxInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument MiniMax
MiniMaxInstrumentor().instrument(tracer_provider=provider)

# Create MiniMax client using OpenAI SDK
client = OpenAI(
    api_key=os.environ.get("MINIMAX_API_KEY", "your-minimax-api-key"),
    base_url="https://api.minimax.io/v1",
)


def simple_chat():
    """Simple chat completion."""
    print("=== Simple Chat ===")
    response = client.chat.completions.create(
        model="MiniMax-M2.7",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is machine learning?"},
        ],
        temperature=0.7,
        max_tokens=1024,
    )
    print(response.choices[0].message.content)
    print()


def streaming_chat():
    """Streaming chat completion."""
    print("=== Streaming Chat ===")
    stream = client.chat.completions.create(
        model="MiniMax-M2.7",
        messages=[{"role": "user", "content": "Tell me a short story"}],
        stream=True,
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print("\n")


def function_calling():
    """Function calling example."""
    print("=== Function Calling ===")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city name",
                        },
                    },
                    "required": ["location"],
                },
            },
        }
    ]

    response = client.chat.completions.create(
        model="MiniMax-M2.7",
        messages=[{"role": "user", "content": "What's the weather in Paris?"}],
        tools=tools,
        tool_choice="auto",
    )

    message = response.choices[0].message
    if message.tool_calls:
        for tool_call in message.tool_calls:
            print(f"Function: {tool_call.function.name}")
            print(f"Arguments: {tool_call.function.arguments}")
    else:
        print(message.content)
    print()


def highspeed_chat():
    """Using MiniMax-M2.7-highspeed for faster inference."""
    print("=== Highspeed Chat ===")
    response = client.chat.completions.create(
        model="MiniMax-M2.7-highspeed",
        messages=[
            {"role": "user", "content": "Summarize the key features of Python in 3 bullet points."},
        ],
        temperature=0.5,
    )
    print(response.choices[0].message.content)
    print()


if __name__ == "__main__":
    simple_chat()
    streaming_chat()
    function_calling()
    highspeed_chat()
