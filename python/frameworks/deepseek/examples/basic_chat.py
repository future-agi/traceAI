"""Basic DeepSeek chat example with tracing."""
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
    # Initialize DeepSeek client via OpenAI SDK
    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY", "your-api-key"),
        base_url="https://api.deepseek.com/v1"
    )

    # Simple chat
    print("=== Simple Chat ===")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"}
        ],
        temperature=0.7,
        max_tokens=100
    )
    print(f"Response: {response.choices[0].message.content}\n")
    print(f"Tokens - Prompt: {response.usage.prompt_tokens}, Completion: {response.usage.completion_tokens}\n")

    # Streaming chat
    print("=== Streaming Chat ===")
    stream = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "user", "content": "Count from 1 to 5."}
        ],
        stream=True
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print("\n")

    # DeepSeek Reasoner (R1) - if available
    print("=== DeepSeek Reasoner (R1) ===")
    try:
        response = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "user", "content": "What is 15% of 240? Show your reasoning."}
            ]
        )
        message = response.choices[0].message

        # R1 models return reasoning_content
        if hasattr(message, 'reasoning_content') and message.reasoning_content:
            print(f"Reasoning: {message.reasoning_content}\n")
        print(f"Answer: {message.content}\n")

        # Check for reasoning tokens
        if hasattr(response.usage, 'completion_tokens_details'):
            details = response.usage.completion_tokens_details
            if hasattr(details, 'reasoning_tokens'):
                print(f"Reasoning tokens: {details.reasoning_tokens}")
    except Exception as e:
        print(f"Reasoner model not available or error: {e}\n")

    # Function calling
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
                            "description": "The city name"
                        }
                    },
                    "required": ["location"]
                }
            }
        }
    ]

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "user", "content": "What's the weather in Tokyo?"}
        ],
        tools=tools,
        tool_choice="auto"
    )

    message = response.choices[0].message
    if message.tool_calls:
        for tool_call in message.tool_calls:
            print(f"Tool: {tool_call.function.name}")
            print(f"Arguments: {tool_call.function.arguments}")
    else:
        print(f"Response: {message.content}")
    print()


if __name__ == "__main__":
    main()
