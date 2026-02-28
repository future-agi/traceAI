"""HuggingFace feature extraction (embeddings) example with tracing."""
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


def cosine_similarity(a: list, b: list) -> float:
    """Calculate cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot_product / (norm_a * norm_b) if norm_a and norm_b else 0


def main():
    client = InferenceClient()

    # Single embedding
    print("=== Single Embedding ===")
    embedding = client.feature_extraction(
        text="Hello, world!",
        model="sentence-transformers/all-MiniLM-L6-v2",
    )
    # Convert to list if needed (may be numpy array)
    if hasattr(embedding, "tolist"):
        embedding = embedding.tolist()

    # Handle nested structure
    if isinstance(embedding[0], list):
        embedding = embedding[0]

    print(f"Embedding dimensions: {len(embedding)}")
    print(f"First 5 values: {embedding[:5]}")

    # Semantic similarity
    print("\n=== Semantic Similarity ===")
    sentences = [
        "The cat sat on the mat.",
        "A feline rested on a rug.",
        "The weather is sunny today.",
    ]

    embeddings = []
    for sentence in sentences:
        emb = client.feature_extraction(
            text=sentence,
            model="sentence-transformers/all-MiniLM-L6-v2",
        )
        if hasattr(emb, "tolist"):
            emb = emb.tolist()
        if isinstance(emb[0], list):
            emb = emb[0]
        embeddings.append(emb)
        print(f"Embedded: {sentence}")

    print("\nSimilarities:")
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            sim = cosine_similarity(embeddings[i], embeddings[j])
            print(f"  {sim:.4f}: '{sentences[i][:30]}...' vs '{sentences[j][:30]}...'")

    # Semantic search
    print("\n=== Semantic Search ===")
    documents = [
        "Python is a popular programming language.",
        "Machine learning enables pattern recognition.",
        "The Eiffel Tower stands in Paris.",
        "Neural networks power modern AI systems.",
    ]

    doc_embeddings = []
    for doc in documents:
        emb = client.feature_extraction(
            text=doc,
            model="sentence-transformers/all-MiniLM-L6-v2",
        )
        if hasattr(emb, "tolist"):
            emb = emb.tolist()
        if isinstance(emb[0], list):
            emb = emb[0]
        doc_embeddings.append(emb)

    query = "What is artificial intelligence?"
    query_emb = client.feature_extraction(
        text=query,
        model="sentence-transformers/all-MiniLM-L6-v2",
    )
    if hasattr(query_emb, "tolist"):
        query_emb = query_emb.tolist()
    if isinstance(query_emb[0], list):
        query_emb = query_emb[0]

    print(f"Query: {query}")
    print("Results:")
    similarities = []
    for i, doc_emb in enumerate(doc_embeddings):
        sim = cosine_similarity(query_emb, doc_emb)
        similarities.append((sim, documents[i]))

    similarities.sort(reverse=True)
    for sim, doc in similarities:
        print(f"  {sim:.4f}: {doc}")


if __name__ == "__main__":
    main()
