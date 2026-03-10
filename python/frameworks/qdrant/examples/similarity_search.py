"""
Similarity Search Example with Qdrant

This example demonstrates vector similarity search using Qdrant with tracing.

Prerequisites:
    pip install traceai-qdrant qdrant-client

Environment variables:
    QDRANT_URL: Your Qdrant server URL (default: http://localhost:6333)
    FI_API_KEY: Your Future AGI API key (optional)
    FI_SECRET_KEY: Your Future AGI secret key (optional)
"""

import os
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_qdrant import QdrantInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="qdrant-similarity-search"
)

# Instrument Qdrant
QdrantInstrumentor().instrument(tracer_provider=trace_provider)

# Create Qdrant client
# Use ":memory:" for in-memory storage (demo), or URL for server
client = QdrantClient(":memory:")

COLLECTION_NAME = "documents"
VECTOR_SIZE = 384  # Simulated embedding size


def setup_collection():
    """Create collection if it doesn't exist."""
    collections = client.get_collections().collections
    collection_names = [c.name for c in collections]

    if COLLECTION_NAME not in collection_names:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )
        print(f"Created collection: {COLLECTION_NAME}")


def generate_fake_embedding(text: str) -> list[float]:
    """Generate a fake embedding for demo purposes."""
    import hashlib
    # Create deterministic pseudo-random embedding from text
    hash_bytes = hashlib.sha256(text.encode()).digest()
    embedding = []
    for i in range(VECTOR_SIZE):
        byte_val = hash_bytes[i % len(hash_bytes)]
        embedding.append((byte_val / 255.0) * 2 - 1)  # Normalize to [-1, 1]
    return embedding


def add_documents(documents: list[dict]):
    """Add documents to Qdrant."""
    points = []
    for i, doc in enumerate(documents):
        embedding = generate_fake_embedding(doc["text"])
        points.append(
            PointStruct(
                id=i,
                vector=embedding,
                payload={
                    "text": doc["text"],
                    "category": doc.get("category", "general"),
                }
            )
        )

    # Upsert points (traced)
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
    )
    print(f"Added {len(points)} documents")


def search(query: str, limit: int = 5, score_threshold: float = 0.0) -> list[dict]:
    """Search for similar documents."""
    query_vector = generate_fake_embedding(query)

    # Search (traced)
    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=limit,
        score_threshold=score_threshold,
        with_payload=True,
    )

    return [
        {
            "id": r.id,
            "score": r.score,
            "text": r.payload.get("text", ""),
            "category": r.payload.get("category", ""),
        }
        for r in results
    ]


def search_with_filter(query: str, category: str, limit: int = 5) -> list[dict]:
    """Search with category filter."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    query_vector = generate_fake_embedding(query)

    # Search with filter (traced)
    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=limit,
        query_filter=Filter(
            must=[
                FieldCondition(key="category", match=MatchValue(value=category))
            ]
        ),
        with_payload=True,
    )

    return [
        {
            "id": r.id,
            "score": r.score,
            "text": r.payload.get("text", ""),
            "category": r.payload.get("category", ""),
        }
        for r in results
    ]


def recommend(positive_ids: list[int], negative_ids: list[int] = None, limit: int = 5) -> list[dict]:
    """Get recommendations based on positive and negative examples."""
    # Recommend (traced)
    results = client.recommend(
        collection_name=COLLECTION_NAME,
        positive=positive_ids,
        negative=negative_ids or [],
        limit=limit,
        with_payload=True,
    )

    return [
        {
            "id": r.id,
            "score": r.score,
            "text": r.payload.get("text", ""),
        }
        for r in results
    ]


if __name__ == "__main__":
    # Setup
    setup_collection()

    # Example documents
    docs = [
        {"text": "Machine learning enables computers to learn from data.", "category": "ml"},
        {"text": "Deep learning is a subset of machine learning using neural networks.", "category": "ml"},
        {"text": "Natural language processing helps computers understand human language.", "category": "nlp"},
        {"text": "Computer vision allows machines to interpret visual information.", "category": "cv"},
        {"text": "Reinforcement learning trains agents through reward signals.", "category": "ml"},
    ]

    # Add documents
    add_documents(docs)

    # Search
    query = "neural networks and deep learning"
    print(f"\nSearching for: '{query}'")
    results = search(query, limit=3)

    print("\nTop results:")
    for r in results:
        print(f"  - [{r['category']}] {r['text'][:50]}... (score: {r['score']:.4f})")

    # Search with filter
    print(f"\nSearching in 'ml' category:")
    filtered_results = search_with_filter(query, category="ml", limit=2)
    for r in filtered_results:
        print(f"  - {r['text'][:50]}...")

    # Get recommendations
    print(f"\nRecommendations based on first document:")
    recommendations = recommend(positive_ids=[0], limit=2)
    for r in recommendations:
        print(f"  - {r['text'][:50]}...")
