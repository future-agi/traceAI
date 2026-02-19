"""
Vector Search Example with Milvus

This example demonstrates vector similarity search using Milvus with tracing.

Prerequisites:
    pip install traceai-milvus pymilvus

Environment variables:
    MILVUS_URI: Your Milvus server URI (default: localhost:19530)
    FI_API_KEY: Your Future AGI API key (optional)
"""

from pymilvus import MilvusClient, DataType
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_milvus import MilvusInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="milvus-vector-search"
)

# Instrument Milvus
MilvusInstrumentor().instrument(tracer_provider=trace_provider)

# Create Milvus client (using lite for demo, use URI for server)
client = MilvusClient("milvus_demo.db")  # Lite mode, file-based

COLLECTION_NAME = "documents"
DIMENSION = 384


def setup_collection():
    """Create collection with schema."""
    # Drop if exists
    if client.has_collection(COLLECTION_NAME):
        client.drop_collection(COLLECTION_NAME)

    # Create collection (traced)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        dimension=DIMENSION,
    )
    print(f"Created collection: {COLLECTION_NAME}")


def generate_fake_embedding(text: str) -> list[float]:
    """Generate fake embedding for demo."""
    import hashlib
    hash_bytes = hashlib.sha256(text.encode()).digest()
    embedding = []
    for i in range(DIMENSION):
        byte_val = hash_bytes[i % len(hash_bytes)]
        embedding.append((byte_val / 255.0) * 2 - 1)
    return embedding


def add_documents(documents: list[dict]):
    """Add documents to Milvus."""
    data = []
    for i, doc in enumerate(documents):
        data.append({
            "id": i,
            "vector": generate_fake_embedding(doc["text"]),
            "text": doc["text"],
            "category": doc.get("category", "general"),
        })

    # Insert (traced)
    client.insert(collection_name=COLLECTION_NAME, data=data)
    print(f"Inserted {len(data)} documents")


def search(query: str, limit: int = 5) -> list[dict]:
    """Search for similar documents."""
    query_vector = generate_fake_embedding(query)

    # Search (traced)
    results = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_vector],
        limit=limit,
        output_fields=["text", "category"],
    )

    formatted = []
    for hits in results:
        for hit in hits:
            formatted.append({
                "id": hit["id"],
                "distance": hit["distance"],
                "text": hit["entity"].get("text", ""),
                "category": hit["entity"].get("category", ""),
            })

    return formatted


def search_with_filter(query: str, category: str, limit: int = 5) -> list[dict]:
    """Search with category filter."""
    query_vector = generate_fake_embedding(query)

    # Search with filter (traced)
    results = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_vector],
        limit=limit,
        filter=f'category == "{category}"',
        output_fields=["text", "category"],
    )

    formatted = []
    for hits in results:
        for hit in hits:
            formatted.append({
                "id": hit["id"],
                "distance": hit["distance"],
                "text": hit["entity"].get("text", ""),
            })

    return formatted


def get_by_ids(ids: list[int]) -> list[dict]:
    """Get documents by IDs."""
    # Get (traced)
    results = client.get(
        collection_name=COLLECTION_NAME,
        ids=ids,
        output_fields=["text", "category"],
    )

    return [
        {
            "id": r["id"],
            "text": r.get("text", ""),
            "category": r.get("category", ""),
        }
        for r in results
    ]


def delete_documents(ids: list[int]):
    """Delete documents by IDs."""
    # Delete (traced)
    client.delete(collection_name=COLLECTION_NAME, ids=ids)
    print(f"Deleted {len(ids)} documents")


if __name__ == "__main__":
    # Setup
    setup_collection()

    # Example documents
    docs = [
        {"text": "Machine learning algorithms learn patterns from data.", "category": "ml"},
        {"text": "Deep neural networks have multiple hidden layers.", "category": "dl"},
        {"text": "Natural language processing understands human language.", "category": "nlp"},
        {"text": "Computer vision processes images and videos.", "category": "cv"},
        {"text": "Reinforcement learning uses reward-based training.", "category": "ml"},
    ]

    # Add documents
    add_documents(docs)

    # Search
    query = "deep learning neural networks"
    print(f"\nSearching for: '{query}'")
    results = search(query, limit=3)

    print("\nTop results:")
    for r in results:
        print(f"  - [{r['category']}] {r['text'][:40]}... (distance: {r['distance']:.4f})")

    # Search with filter
    print(f"\nFiltered search (category='ml'):")
    filtered = search_with_filter(query, category="ml", limit=2)
    for r in filtered:
        print(f"  - {r['text'][:50]}...")

    # Get by IDs
    print(f"\nGet documents by ID [0, 1]:")
    docs = get_by_ids([0, 1])
    for d in docs:
        print(f"  - [{d['id']}] {d['text'][:40]}...")

    # Cleanup
    client.drop_collection(COLLECTION_NAME)
