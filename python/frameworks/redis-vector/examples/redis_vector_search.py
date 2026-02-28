"""
Redis Vector Search Example

This example demonstrates vector similarity search using Redis with RediSearch.

Prerequisites:
    pip install traceai-redis redis redisvl

Environment variables:
    REDIS_URL: Redis connection URL (default: redis://localhost:6379)
    FI_API_KEY: Your Future AGI API key (optional)
"""

import os
import numpy as np
from redis import Redis
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_redis import RedisInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="redis-vector-search"
)

# Instrument Redis
RedisInstrumentor().instrument(tracer_provider=trace_provider)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
INDEX_NAME = "idx:documents"
DIMENSION = 384


def get_redis_client():
    """Get Redis client."""
    return Redis.from_url(REDIS_URL, decode_responses=True)


def generate_fake_embedding(text: str) -> list[float]:
    """Generate fake embedding for demo."""
    import hashlib
    hash_bytes = hashlib.sha256(text.encode()).digest()
    embedding = []
    for i in range(DIMENSION):
        byte_val = hash_bytes[i % len(hash_bytes)]
        embedding.append((byte_val / 255.0) * 2 - 1)
    return embedding


def setup_index():
    """Create RediSearch index with vector field."""
    client = get_redis_client()

    # Drop existing index if exists
    try:
        client.execute_command("FT.DROPINDEX", INDEX_NAME)
    except:
        pass

    # Create index schema (traced via FT.CREATE)
    client.execute_command(
        "FT.CREATE", INDEX_NAME,
        "ON", "HASH",
        "PREFIX", "1", "doc:",
        "SCHEMA",
        "text", "TEXT",
        "category", "TAG",
        "embedding", "VECTOR", "FLAT", "6",
        "TYPE", "FLOAT32",
        "DIM", str(DIMENSION),
        "DISTANCE_METRIC", "COSINE"
    )
    print(f"Created index: {INDEX_NAME}")


def add_documents(documents: list[dict]):
    """Add documents with embeddings using HSET."""
    client = get_redis_client()

    for i, doc in enumerate(documents):
        key = f"doc:{i}"
        embedding = generate_fake_embedding(doc["text"])
        embedding_bytes = np.array(embedding, dtype=np.float32).tobytes()

        # HSET with vector (traced)
        client.hset(
            key,
            mapping={
                "text": doc["text"],
                "category": doc.get("category", "general"),
                "embedding": embedding_bytes,
            }
        )

    print(f"Added {len(documents)} documents")


def vector_search(query: str, limit: int = 5) -> list[dict]:
    """Search for similar documents using KNN."""
    client = get_redis_client()

    query_vector = generate_fake_embedding(query)
    query_bytes = np.array(query_vector, dtype=np.float32).tobytes()

    # FT.SEARCH with KNN (traced)
    # Query format: *=>[KNN $K @field $BLOB AS score]
    query_str = f"*=>[KNN {limit} @embedding $vec AS score]"

    results = client.execute_command(
        "FT.SEARCH", INDEX_NAME,
        query_str,
        "PARAMS", "2", "vec", query_bytes,
        "SORTBY", "score",
        "RETURN", "3", "text", "category", "score",
        "DIALECT", "2"
    )

    # Parse results
    formatted = []
    if results and len(results) > 1:
        total = results[0]
        i = 1
        while i < len(results):
            doc_id = results[i]
            fields = results[i + 1] if i + 1 < len(results) else []

            field_dict = {}
            for j in range(0, len(fields), 2):
                if j + 1 < len(fields):
                    field_dict[fields[j]] = fields[j + 1]

            formatted.append({
                "id": doc_id,
                "text": field_dict.get("text", ""),
                "category": field_dict.get("category", ""),
                "score": float(field_dict.get("score", 0)),
            })
            i += 2

    return formatted


def search_with_filter(query: str, category: str, limit: int = 5) -> list[dict]:
    """Vector search with category filter."""
    client = get_redis_client()

    query_vector = generate_fake_embedding(query)
    query_bytes = np.array(query_vector, dtype=np.float32).tobytes()

    # FT.SEARCH with filter (traced)
    query_str = f"(@category:{{{category}}})=>[KNN {limit} @embedding $vec AS score]"

    results = client.execute_command(
        "FT.SEARCH", INDEX_NAME,
        query_str,
        "PARAMS", "2", "vec", query_bytes,
        "SORTBY", "score",
        "RETURN", "3", "text", "category", "score",
        "DIALECT", "2"
    )

    # Parse results
    formatted = []
    if results and len(results) > 1:
        i = 1
        while i < len(results):
            doc_id = results[i]
            fields = results[i + 1] if i + 1 < len(results) else []

            field_dict = {}
            for j in range(0, len(fields), 2):
                if j + 1 < len(fields):
                    field_dict[fields[j]] = fields[j + 1]

            formatted.append({
                "id": doc_id,
                "text": field_dict.get("text", ""),
                "score": float(field_dict.get("score", 0)),
            })
            i += 2

    return formatted


def delete_document(doc_id: str):
    """Delete a document."""
    client = get_redis_client()
    client.delete(doc_id)
    print(f"Deleted {doc_id}")


if __name__ == "__main__":
    # Setup
    setup_index()

    # Example documents
    docs = [
        {"text": "Machine learning enables pattern recognition.", "category": "ml"},
        {"text": "Deep learning uses neural network layers.", "category": "dl"},
        {"text": "Redis is an in-memory data store.", "category": "db"},
        {"text": "Vector search finds similar embeddings.", "category": "db"},
        {"text": "Natural language processing analyzes text.", "category": "nlp"},
    ]

    # Add documents
    add_documents(docs)

    # Vector search
    query = "neural networks and deep learning"
    print(f"\nVector search for: '{query}'")
    results = vector_search(query, limit=3)

    print("\nTop results:")
    for r in results:
        print(f"  - [{r['category']}] {r['text'][:40]}... (score: {r['score']:.4f})")

    # Filtered search
    print(f"\nFiltered search (category='db'):")
    filtered = search_with_filter(query, category="db", limit=2)
    for r in filtered:
        print(f"  - {r['text'][:50]}... (score: {r['score']:.4f})")
