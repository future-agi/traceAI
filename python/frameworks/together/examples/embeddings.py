"""Together AI embeddings example with tracing."""
import os
from together import Together
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_together import TogetherInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument Together
TogetherInstrumentor().instrument(tracer_provider=provider)


def cosine_similarity(a: list, b: list) -> float:
    """Calculate cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot_product / (norm_a * norm_b) if norm_a and norm_b else 0


def main():
    client = Together()

    # Single embedding
    print("=== Single Embedding ===")
    response = client.embeddings.create(
        model="togethercomputer/m2-bert-80M-8k-retrieval",
        input="Hello, world!",
    )
    embedding = response.data[0].embedding
    print(f"Embedding dimensions: {len(embedding)}")
    print(f"First 5 values: {embedding[:5]}")

    # Batch embeddings
    print("\n=== Batch Embeddings ===")
    documents = [
        "Python is a versatile programming language.",
        "Machine learning automates data analysis.",
        "Deep learning uses neural networks.",
        "Natural language processing understands text.",
    ]

    doc_response = client.embeddings.create(
        model="togethercomputer/m2-bert-80M-8k-retrieval",
        input=documents,
    )
    print(f"Embedded {len(documents)} documents")
    print(f"Total tokens used: {doc_response.usage.total_tokens}")

    # Semantic search
    print("\n=== Semantic Search ===")
    query = "How do neural networks work?"
    query_response = client.embeddings.create(
        model="togethercomputer/m2-bert-80M-8k-retrieval",
        input=query,
    )
    query_embedding = query_response.data[0].embedding

    print(f"Query: {query}")
    print("Results:")
    similarities = []
    for i, doc_data in enumerate(doc_response.data):
        similarity = cosine_similarity(query_embedding, doc_data.embedding)
        similarities.append((similarity, documents[i]))

    similarities.sort(reverse=True)
    for sim, doc in similarities:
        print(f"  {sim:.4f}: {doc}")

    # RAG-style usage
    print("\n=== RAG Context Building ===")
    top_docs = [doc for _, doc in similarities[:2]]
    context = "\n".join(top_docs)

    # Generate answer with context
    chat_response = client.chat.completions.create(
        model="meta-llama/Llama-3-8b-chat-hf",
        messages=[
            {
                "role": "system",
                "content": f"Answer based on this context:\n{context}",
            },
            {"role": "user", "content": query},
        ],
        max_tokens=200,
    )
    print(f"Answer: {chat_response.choices[0].message.content}")


if __name__ == "__main__":
    main()
