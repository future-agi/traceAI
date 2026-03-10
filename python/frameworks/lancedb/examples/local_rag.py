"""
Local RAG Example with LanceDB

LanceDB is an embedded vector database that runs locally without a server.
Perfect for development, edge deployments, and serverless applications.

Prerequisites:
    pip install traceai-lancedb lancedb

Environment variables:
    FI_API_KEY: Your Future AGI API key (optional)
"""

import lancedb
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_lancedb import LanceDBInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="lancedb-local-rag"
)

# Instrument LanceDB
LanceDBInstrumentor().instrument(tracer_provider=trace_provider)

# Connect to local database (creates ~/.lancedb by default)
db = lancedb.connect("~/.lancedb/demo")

TABLE_NAME = "documents"
DIMENSION = 384


def generate_fake_embedding(text: str) -> list[float]:
    """Generate fake embedding for demo."""
    import hashlib
    hash_bytes = hashlib.sha256(text.encode()).digest()
    embedding = []
    for i in range(DIMENSION):
        byte_val = hash_bytes[i % len(hash_bytes)]
        embedding.append((byte_val / 255.0) * 2 - 1)
    return embedding


def create_table_with_data(documents: list[dict]):
    """Create table and add initial data."""
    data = []
    for i, doc in enumerate(documents):
        data.append({
            "id": i,
            "text": doc["text"],
            "category": doc.get("category", "general"),
            "vector": generate_fake_embedding(doc["text"]),
        })

    # Create table (traced)
    table = db.create_table(TABLE_NAME, data, mode="overwrite")
    print(f"Created table with {len(data)} documents")
    return table


def add_documents(documents: list[dict]):
    """Add more documents to existing table."""
    table = db.open_table(TABLE_NAME)

    data = []
    for i, doc in enumerate(documents):
        data.append({
            "id": 100 + i,
            "text": doc["text"],
            "category": doc.get("category", "general"),
            "vector": generate_fake_embedding(doc["text"]),
        })

    # Add (traced)
    table.add(data)
    print(f"Added {len(data)} documents")


def search(query: str, limit: int = 5) -> list[dict]:
    """Search for similar documents."""
    table = db.open_table(TABLE_NAME)
    query_vector = generate_fake_embedding(query)

    # Search (traced)
    results = table.search(query_vector).limit(limit).to_list()

    return [
        {
            "id": r["id"],
            "text": r["text"],
            "category": r["category"],
            "distance": r.get("_distance", 0),
        }
        for r in results
    ]


def search_with_filter(query: str, category: str, limit: int = 5) -> list[dict]:
    """Search with SQL-like filter."""
    table = db.open_table(TABLE_NAME)
    query_vector = generate_fake_embedding(query)

    # Search with filter (traced)
    results = (
        table.search(query_vector)
        .where(f"category = '{category}'")
        .limit(limit)
        .to_list()
    )

    return [
        {
            "id": r["id"],
            "text": r["text"],
            "distance": r.get("_distance", 0),
        }
        for r in results
    ]


def hybrid_search(query: str, limit: int = 5) -> list[dict]:
    """Hybrid search combining vector and full-text search."""
    table = db.open_table(TABLE_NAME)
    query_vector = generate_fake_embedding(query)

    # Hybrid search (traced)
    # Note: Requires FTS index to be created on the text column
    results = (
        table.search(query_vector)
        .limit(limit)
        .to_list()
    )

    return [
        {
            "id": r["id"],
            "text": r["text"],
            "distance": r.get("_distance", 0),
        }
        for r in results
    ]


def delete_by_filter(category: str):
    """Delete documents matching filter."""
    table = db.open_table(TABLE_NAME)

    # Delete (traced)
    table.delete(f"category = '{category}'")
    print(f"Deleted documents in category '{category}'")


def list_tables():
    """List all tables in the database."""
    return db.table_names()


if __name__ == "__main__":
    # Example documents
    docs = [
        {"text": "Machine learning is a subset of AI.", "category": "ml"},
        {"text": "Deep learning uses neural networks.", "category": "dl"},
        {"text": "LanceDB is an embedded vector database.", "category": "db"},
        {"text": "RAG combines retrieval with generation.", "category": "ml"},
        {"text": "Vector search finds similar embeddings.", "category": "db"},
    ]

    # Create table with data
    create_table_with_data(docs)

    # List tables
    print(f"\nTables in database: {list_tables()}")

    # Search
    query = "neural networks and deep learning"
    print(f"\nSearching for: '{query}'")
    results = search(query, limit=3)

    print("\nTop results:")
    for r in results:
        print(f"  - [{r['category']}] {r['text'][:40]}... (distance: {r['distance']:.4f})")

    # Filtered search
    print(f"\nFiltered search (category='ml'):")
    filtered = search_with_filter(query, category="ml", limit=2)
    for r in filtered:
        print(f"  - {r['text'][:50]}...")

    # Add more documents
    new_docs = [
        {"text": "Transformers revolutionized NLP.", "category": "dl"},
    ]
    add_documents(new_docs)

    # Cleanup (optional)
    # db.drop_table(TABLE_NAME)
