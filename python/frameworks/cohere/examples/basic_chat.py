"""Basic Cohere chat example with tracing."""
import cohere
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_cohere import CohereInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument Cohere
CohereInstrumentor().instrument(tracer_provider=provider)


def main():
    client = cohere.Client()

    # Simple chat
    print("=== Simple Chat ===")
    response = client.chat(
        model="command-r-plus",
        message="What is the capital of France?",
    )
    print(f"Response: {response.text}\n")

    # Chat with history
    print("=== Chat with History ===")
    response = client.chat(
        model="command-r-plus",
        message="What's my name?",
        chat_history=[
            {"role": "USER", "message": "My name is Alice"},
            {"role": "CHATBOT", "message": "Hello Alice! Nice to meet you."},
        ],
    )
    print(f"Response: {response.text}\n")

    # Streaming chat
    print("=== Streaming Chat ===")
    for event in client.chat_stream(
        model="command-r-plus",
        message="Count to 5.",
    ):
        if event.event_type == "text-generation":
            print(event.text, end="", flush=True)
    print("\n")


if __name__ == "__main__":
    main()
