"""Basic HuggingFace Inference API example with tracing."""
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
    # Initialize client (uses HF_TOKEN environment variable)
    client = InferenceClient()

    # Text Generation
    print("=== Text Generation ===")
    response = client.text_generation(
        "The capital of France is",
        model="meta-llama/Llama-2-7b-chat-hf",
        max_new_tokens=50,
        temperature=0.7,
    )
    print(f"Response: {response}\n")

    # Chat Completion
    print("=== Chat Completion ===")
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is machine learning in one sentence?"},
    ]
    response = client.chat_completion(
        messages=messages,
        model="meta-llama/Llama-2-7b-chat-hf",
        max_tokens=100,
    )
    print(f"Response: {response.choices[0].message.content}\n")

    # Feature Extraction (Embeddings)
    print("=== Feature Extraction (Embeddings) ===")
    embedding = client.feature_extraction(
        "Hello, world!",
        model="sentence-transformers/all-MiniLM-L6-v2",
    )
    print(f"Embedding dimensions: {len(embedding[0])}")
    print(f"First 5 values: {embedding[0][:5]}\n")

    # Streaming Text Generation
    print("=== Streaming Text Generation ===")
    print("Response: ", end="")
    for chunk in client.text_generation(
        "Count from 1 to 5:",
        model="meta-llama/Llama-2-7b-chat-hf",
        max_new_tokens=50,
        stream=True,
    ):
        print(chunk, end="", flush=True)
    print("\n")


if __name__ == "__main__":
    main()
