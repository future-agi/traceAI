"""RAG pipeline example with Ollama and tracing."""
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


def cosine_similarity(a: list, b: list) -> float:
    """Calculate cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot_product / (norm_a * norm_b) if norm_a and norm_b else 0


def main():
    # Knowledge base
    documents = [
        "Python is a high-level programming language known for its readability.",
        "Machine learning is a subset of artificial intelligence.",
        "The Eiffel Tower is located in Paris, France.",
        "Natural language processing enables computers to understand human language.",
        "Deep learning uses neural networks with many layers.",
    ]

    # Embed all documents
    print("=== Embedding Documents ===")
    doc_embeddings = []
    for doc in documents:
        response = ollama.embed(model="nomic-embed-text", input=doc)
        doc_embeddings.append(response["embeddings"][0])
        print(f"Embedded: {doc[:50]}...")

    # User query
    query = "What is machine learning?"
    print(f"\n=== Query: {query} ===")

    # Embed query
    query_response = ollama.embed(model="nomic-embed-text", input=query)
    query_embedding = query_response["embeddings"][0]

    # Find most relevant documents
    similarities = [
        (i, cosine_similarity(query_embedding, doc_emb))
        for i, doc_emb in enumerate(doc_embeddings)
    ]
    similarities.sort(key=lambda x: x[1], reverse=True)

    print("\n=== Top Relevant Documents ===")
    top_docs = []
    for idx, score in similarities[:3]:
        print(f"Score: {score:.4f} - {documents[idx]}")
        top_docs.append(documents[idx])

    # Generate response with context
    print("\n=== Generating Response ===")
    context = "\n".join(top_docs)
    response = ollama.chat(
        model="llama3.2",
        messages=[
            {
                "role": "system",
                "content": f"Answer the question based on this context:\n{context}",
            },
            {"role": "user", "content": query},
        ],
    )
    print(f"Answer: {response['message']['content']}")

    # Show token usage
    print(f"\nTokens - Prompt: {response.get('prompt_eval_count', 'N/A')}")
    print(f"Tokens - Completion: {response.get('eval_count', 'N/A')}")


if __name__ == "__main__":
    main()
