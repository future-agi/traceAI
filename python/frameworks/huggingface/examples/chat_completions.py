"""HuggingFace chat completions example with tracing."""
import os
from huggingface_hub import InferenceClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_huggingface import HuggingFaceInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument HuggingFace
HuggingFaceInstrumentor().instrument(tracer_provider=provider)


def main():
    client = InferenceClient()

    # Simple chat
    print("=== Simple Chat ===")
    response = client.chat_completion(
        model="meta-llama/Llama-3.2-3B-Instruct",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
        ],
        max_tokens=100,
    )
    print(f"Response: {response.choices[0].message.content}")
    print(f"Tokens - Prompt: {response.usage.prompt_tokens}")
    print(f"Tokens - Completion: {response.usage.completion_tokens}")

    # Multi-turn conversation
    print("\n=== Multi-turn Conversation ===")
    messages = [
        {"role": "system", "content": "You are a friendly tutor."},
        {"role": "user", "content": "My name is Alice."},
    ]
    response = client.chat_completion(
        model="meta-llama/Llama-3.2-3B-Instruct",
        messages=messages,
        max_tokens=50,
    )
    print(f"Assistant: {response.choices[0].message.content}")

    messages.append({"role": "assistant", "content": response.choices[0].message.content})
    messages.append({"role": "user", "content": "What's my name?"})

    response = client.chat_completion(
        model="meta-llama/Llama-3.2-3B-Instruct",
        messages=messages,
        max_tokens=50,
    )
    print(f"Assistant: {response.choices[0].message.content}")

    # Streaming response
    print("\n=== Streaming Response ===")
    stream = client.chat_completion(
        model="meta-llama/Llama-3.2-3B-Instruct",
        messages=[{"role": "user", "content": "Count from 1 to 5."}],
        max_tokens=100,
        stream=True,
    )
    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()

    # Code generation
    print("\n=== Code Generation ===")
    response = client.chat_completion(
        model="codellama/CodeLlama-7b-Instruct-hf",
        messages=[
            {
                "role": "system",
                "content": "You are an expert Python programmer. Write clean, documented code.",
            },
            {
                "role": "user",
                "content": "Write a function to check if a number is prime.",
            },
        ],
        max_tokens=300,
    )
    print(f"Code:\n{response.choices[0].message.content}")


if __name__ == "__main__":
    main()
