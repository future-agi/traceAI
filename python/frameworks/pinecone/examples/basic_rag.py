"""
Basic RAG Example with Pinecone and OpenAI

This example demonstrates how to build a simple RAG pipeline with:
1. Pinecone vector database instrumentation
2. OpenAI for embeddings and completions

Prerequisites:
    pip install traceai-pinecone traceai-openai pinecone-client openai

Environment variables:
    PINECONE_API_KEY: Your Pinecone API key
    OPENAI_API_KEY: Your OpenAI API key
    FI_API_KEY: Your Future AGI API key (optional)
    FI_SECRET_KEY: Your Future AGI secret key (optional)
"""

import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pinecone import PineconeInstrumentor
from traceai_openai import OpenAIInstrumentor
import pinecone
import openai

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="pinecone-rag-example"
)

# Instrument both Pinecone and OpenAI
PineconeInstrumentor().instrument(tracer_provider=trace_provider)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# Initialize clients
pc = pinecone.Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
openai_client = openai.OpenAI()

# Configuration
INDEX_NAME = "rag-demo"
EMBEDDING_MODEL = "text-embedding-3-small"
COMPLETION_MODEL = "gpt-4o-mini"


def get_embedding(text: str) -> list[float]:
    """Generate embedding for text using OpenAI."""
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text
    )
    return response.data[0].embedding


def index_documents(documents: list[dict]):
    """Index documents in Pinecone."""
    index = pc.Index(INDEX_NAME)

    vectors = []
    for i, doc in enumerate(documents):
        embedding = get_embedding(doc["content"])
        vectors.append({
            "id": f"doc_{i}",
            "values": embedding,
            "metadata": {
                "title": doc.get("title", ""),
                "content": doc["content"][:1000],  # Store first 1000 chars
            }
        })

    # Upsert vectors (traced automatically)
    index.upsert(vectors=vectors, namespace="documents")
    print(f"Indexed {len(vectors)} documents")


def search_documents(query: str, top_k: int = 5) -> list[dict]:
    """Search for relevant documents."""
    index = pc.Index(INDEX_NAME)

    # Generate query embedding (traced)
    query_embedding = get_embedding(query)

    # Search Pinecone (traced)
    results = index.query(
        vector=query_embedding,
        top_k=top_k,
        include_metadata=True,
        namespace="documents"
    )

    return [
        {
            "id": match.id,
            "score": match.score,
            "content": match.metadata.get("content", ""),
            "title": match.metadata.get("title", ""),
        }
        for match in results.matches
    ]


def generate_answer(query: str, context_docs: list[dict]) -> str:
    """Generate answer using retrieved context."""
    context = "\n\n".join([
        f"Document: {doc['title']}\n{doc['content']}"
        for doc in context_docs
    ])

    # Generate completion (traced)
    response = openai_client.chat.completions.create(
        model=COMPLETION_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant. Answer questions based on the provided context."
            },
            {
                "role": "user",
                "content": f"Context:\n{context}\n\nQuestion: {query}\n\nAnswer:"
            }
        ],
        temperature=0.7,
        max_tokens=500
    )

    return response.choices[0].message.content


def rag_query(query: str) -> str:
    """Complete RAG pipeline: search + generate."""
    # 1. Search for relevant documents
    docs = search_documents(query, top_k=3)

    if not docs:
        return "No relevant documents found."

    # 2. Generate answer using context
    answer = generate_answer(query, docs)

    return answer


if __name__ == "__main__":
    # Example: Index some documents
    sample_docs = [
        {
            "title": "Introduction to Machine Learning",
            "content": "Machine learning is a subset of artificial intelligence that enables systems to learn and improve from experience without being explicitly programmed."
        },
        {
            "title": "Neural Networks Explained",
            "content": "Neural networks are computing systems inspired by biological neural networks. They consist of interconnected nodes that process information using connectionist approaches."
        },
        {
            "title": "Deep Learning Applications",
            "content": "Deep learning has revolutionized many fields including computer vision, natural language processing, and speech recognition. It uses multiple layers of neural networks."
        }
    ]

    # Note: Uncomment to index documents (requires existing index)
    # index_documents(sample_docs)

    # Example: Query the RAG system
    query = "What is machine learning?"
    print(f"\nQuery: {query}")

    # Search only (without generation)
    results = search_documents(query, top_k=2)
    print(f"\nTop {len(results)} results:")
    for r in results:
        print(f"  - {r['title']} (score: {r['score']:.3f})")

    # Full RAG query (search + generate)
    # answer = rag_query(query)
    # print(f"\nAnswer: {answer}")
