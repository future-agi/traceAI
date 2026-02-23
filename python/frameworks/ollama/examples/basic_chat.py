"""Basic Ollama chat example with tracing."""
import ollama
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_ollama import OllamaInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument Ollama
OllamaInstrumentor().instrument(tracer_provider=provider)


def main():
    # Simple chat
    print("=== Simple Chat ===")
    response = ollama.chat(
        model="llama3.2",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
        ],
    )
    print(f"Response: {response['message']['content']}\n")

    # Multi-turn conversation
    print("=== Multi-turn Conversation ===")
    messages = [
        {"role": "user", "content": "My name is Alice."},
    ]
    response = ollama.chat(model="llama3.2", messages=messages)
    print(f"Assistant: {response['message']['content']}")

    messages.append(response["message"])
    messages.append({"role": "user", "content": "What's my name?"})

    response = ollama.chat(model="llama3.2", messages=messages)
    print(f"Assistant: {response['message']['content']}\n")

    # Streaming
    print("=== Streaming Response ===")
    stream = ollama.chat(
        model="llama3.2",
        messages=[{"role": "user", "content": "Count to 5."}],
        stream=True,
    )
    for chunk in stream:
        print(chunk["message"]["content"], end="", flush=True)
    print("\n")


if __name__ == "__main__":
    main()
