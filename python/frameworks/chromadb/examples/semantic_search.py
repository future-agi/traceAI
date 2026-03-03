"""
Semantic Search Example with ChromaDB

This example demonstrates semantic search using ChromaDB with tracing.
ChromaDB can use built-in embedding functions or external ones.

Prerequisites:
    pip install traceai-chromadb chromadb

Environment variables:
    FI_API_KEY: Your Future AGI API key (optional)
    FI_SECRET_KEY: Your Future AGI secret key (optional)
"""

import chromadb
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_chromadb import ChromaDBInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="chromadb-semantic-search"
)

# Instrument ChromaDB
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

# Create ChromaDB client (ephemeral for demo, use PersistentClient for production)
client = chromadb.Client()

# Create or get collection with default embedding function
collection = client.get_or_create_collection(
    name="documents",
    metadata={"hnsw:space": "cosine"}  # Use cosine similarity
)


def add_documents(documents: list[dict]):
    """Add documents to the collection."""
    ids = [doc["id"] for doc in documents]
    texts = [doc["text"] for doc in documents]
    metadatas = [{"source": doc.get("source", "unknown")} for doc in documents]

    # Add documents (ChromaDB will generate embeddings automatically)
    # This operation is traced
    collection.add(
        ids=ids,
        documents=texts,
        metadatas=metadatas,
    )
    print(f"Added {len(documents)} documents")


def search(query: str, n_results: int = 5) -> list[dict]:
    """Search for similar documents."""
    # Query the collection (traced)
    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    # Format results
    formatted = []
    for i in range(len(results["ids"][0])):
        formatted.append({
            "id": results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
        })

    return formatted


def hybrid_search(query: str, where_filter: dict = None, n_results: int = 5) -> list[dict]:
    """Search with both semantic similarity and metadata filtering."""
    # Query with filters (traced)
    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where_filter,
        include=["documents", "metadatas", "distances"],
    )

    formatted = []
    for i in range(len(results["ids"][0])):
        formatted.append({
            "id": results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
        })

    return formatted


def delete_documents(ids: list[str]):
    """Delete documents by ID."""
    # Delete operation (traced)
    collection.delete(ids=ids)
    print(f"Deleted {len(ids)} documents")


def get_collection_info():
    """Get collection statistics."""
    # Count operation (traced)
    count = collection.count()

    # Peek at some documents (traced)
    sample = collection.peek(limit=3)

    return {
        "total_documents": count,
        "sample_ids": sample["ids"][:3] if sample["ids"] else [],
    }


if __name__ == "__main__":
    # Example documents
    docs = [
        {"id": "doc1", "text": "Python is a high-level programming language known for its simplicity.", "source": "wiki"},
        {"id": "doc2", "text": "Machine learning is a subset of artificial intelligence.", "source": "textbook"},
        {"id": "doc3", "text": "ChromaDB is an open-source embedding database.", "source": "docs"},
        {"id": "doc4", "text": "Vector databases store high-dimensional embeddings for similarity search.", "source": "blog"},
        {"id": "doc5", "text": "Deep learning uses neural networks with multiple layers.", "source": "textbook"},
    ]

    # Add documents
    add_documents(docs)

    # Get collection info
    info = get_collection_info()
    print(f"\nCollection has {info['total_documents']} documents")

    # Semantic search
    query = "What is AI and machine learning?"
    print(f"\nSearching for: '{query}'")
    results = search(query, n_results=3)

    print("\nTop results:")
    for r in results:
        print(f"  - [{r['id']}] {r['document'][:60]}... (distance: {r['distance']:.4f})")

    # Hybrid search with filter
    print("\n\nSearching with filter (source='textbook'):")
    filtered_results = hybrid_search(
        query="neural networks and AI",
        where_filter={"source": "textbook"},
        n_results=2
    )

    for r in filtered_results:
        print(f"  - [{r['id']}] {r['document'][:60]}...")
