"""Ollama embeddings example with tracing."""
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
    # Single embedding
    print("=== Single Embedding ===")
    response = ollama.embed(
        model="nomic-embed-text",
        input="Hello, world!",
    )
    print(f"Embedding dimensions: {len(response['embedding'])}")
    print(f"First 5 values: {response['embedding'][:5]}\n")

    # Batch embeddings
    print("=== Batch Embeddings ===")
    texts = [
        "The quick brown fox jumps over the lazy dog.",
        "Machine learning is a subset of artificial intelligence.",
        "Python is a popular programming language.",
    ]

    for text in texts:
        response = ollama.embed(model="nomic-embed-text", input=text)
        print(f"Text: {text[:40]}...")
        print(f"Embedding dimensions: {len(response['embedding'])}\n")


if __name__ == "__main__":
    main()
