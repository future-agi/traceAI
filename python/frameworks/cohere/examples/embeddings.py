"""Cohere embeddings example with tracing."""
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


def cosine_similarity(a: list, b: list) -> float:
    """Calculate cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot_product / (norm_a * norm_b) if norm_a and norm_b else 0


def main():
    client = cohere.Client()

    # Single embedding
    print("=== Single Embedding ===")
    response = client.embed(
        model="embed-english-v3.0",
        texts=["Hello, world!"],
        input_type="search_query",
    )
    print(f"Embedding dimensions: {len(response.embeddings[0])}")
    print(f"First 5 values: {response.embeddings[0][:5]}")

    # Batch embeddings for semantic search
    print("\n=== Batch Embeddings for Semantic Search ===")
    documents = [
        "Python is a programming language.",
        "Machine learning uses data to make predictions.",
        "The Eiffel Tower is in Paris.",
        "Artificial intelligence mimics human thinking.",
    ]

    # Embed documents
    doc_response = client.embed(
        model="embed-english-v3.0",
        texts=documents,
        input_type="search_document",
    )
    print(f"Embedded {len(documents)} documents")

    # Search query
    query = "What is AI?"
    query_response = client.embed(
        model="embed-english-v3.0",
        texts=[query],
        input_type="search_query",
    )

    # Calculate similarities
    print(f"\nQuery: {query}")
    print("Similarities:")
    query_embedding = query_response.embeddings[0]
    for i, doc_embedding in enumerate(doc_response.embeddings):
        similarity = cosine_similarity(query_embedding, doc_embedding)
        print(f"  {similarity:.4f}: {documents[i]}")

    # Multilingual embeddings
    print("\n=== Multilingual Embeddings ===")
    texts = [
        "Hello, how are you?",
        "Bonjour, comment allez-vous?",
        "Hola, como estas?",
    ]
    multi_response = client.embed(
        model="embed-multilingual-v3.0",
        texts=texts,
        input_type="search_document",
    )
    print(f"Embedded {len(texts)} texts in different languages")

    # Show similarities between languages
    print("Cross-lingual similarities:")
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            sim = cosine_similarity(
                multi_response.embeddings[i], multi_response.embeddings[j]
            )
            print(f"  {sim:.4f}: '{texts[i][:20]}...' vs '{texts[j][:20]}...'")


if __name__ == "__main__":
    main()
