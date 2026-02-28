"""
PostgreSQL Vector Search Example with pgvector

This example demonstrates vector similarity search using pgvector extension.

Prerequisites:
    pip install traceai-pgvector pgvector psycopg2-binary
    PostgreSQL with pgvector extension installed

Environment variables:
    DATABASE_URL: PostgreSQL connection string (default: postgresql://localhost/vectordb)
    FI_API_KEY: Your Future AGI API key (optional)
"""

import os
import psycopg2
from pgvector.psycopg2 import register_vector
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pgvector import PgVectorInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="pgvector-search"
)

# Instrument pgvector
PgVectorInstrumentor().instrument(tracer_provider=trace_provider)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/vectordb")
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


def get_connection():
    """Get database connection with pgvector registered."""
    conn = psycopg2.connect(DATABASE_URL)
    register_vector(conn)
    return conn


def setup_database():
    """Create table with vector column and index."""
    conn = get_connection()
    cur = conn.cursor()

    # Enable pgvector extension
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Create table
    cur.execute(f"""
        DROP TABLE IF EXISTS documents;
        CREATE TABLE documents (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            category VARCHAR(50),
            embedding vector({DIMENSION})
        )
    """)

    # Create HNSW index for faster search
    cur.execute("""
        CREATE INDEX ON documents
        USING hnsw (embedding vector_cosine_ops)
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("Database setup complete")


def add_documents(documents: list[dict]):
    """Add documents with embeddings."""
    conn = get_connection()
    cur = conn.cursor()

    for doc in documents:
        embedding = generate_fake_embedding(doc["text"])
        # Insert with vector (traced)
        cur.execute(
            "INSERT INTO documents (text, category, embedding) VALUES (%s, %s, %s)",
            (doc["text"], doc.get("category", "general"), embedding)
        )

    conn.commit()
    cur.close()
    conn.close()
    print(f"Added {len(documents)} documents")


def search_l2(query: str, limit: int = 5) -> list[dict]:
    """Search using L2 (Euclidean) distance."""
    conn = get_connection()
    cur = conn.cursor()

    query_vector = generate_fake_embedding(query)

    # L2 distance search (traced)
    cur.execute("""
        SELECT id, text, category, embedding <-> %s AS distance
        FROM documents
        ORDER BY embedding <-> %s
        LIMIT %s
    """, (query_vector, query_vector, limit))

    results = [
        {"id": row[0], "text": row[1], "category": row[2], "distance": row[3]}
        for row in cur.fetchall()
    ]

    cur.close()
    conn.close()
    return results


def search_cosine(query: str, limit: int = 5) -> list[dict]:
    """Search using cosine distance."""
    conn = get_connection()
    cur = conn.cursor()

    query_vector = generate_fake_embedding(query)

    # Cosine distance search (traced)
    cur.execute("""
        SELECT id, text, category, embedding <=> %s AS distance
        FROM documents
        ORDER BY embedding <=> %s
        LIMIT %s
    """, (query_vector, query_vector, limit))

    results = [
        {"id": row[0], "text": row[1], "category": row[2], "distance": row[3]}
        for row in cur.fetchall()
    ]

    cur.close()
    conn.close()
    return results


def search_inner_product(query: str, limit: int = 5) -> list[dict]:
    """Search using inner product (for normalized vectors)."""
    conn = get_connection()
    cur = conn.cursor()

    query_vector = generate_fake_embedding(query)

    # Inner product search (traced)
    cur.execute("""
        SELECT id, text, category, (embedding <#> %s) * -1 AS similarity
        FROM documents
        ORDER BY embedding <#> %s
        LIMIT %s
    """, (query_vector, query_vector, limit))

    results = [
        {"id": row[0], "text": row[1], "category": row[2], "similarity": row[3]}
        for row in cur.fetchall()
    ]

    cur.close()
    conn.close()
    return results


def search_with_filter(query: str, category: str, limit: int = 5) -> list[dict]:
    """Search with category filter."""
    conn = get_connection()
    cur = conn.cursor()

    query_vector = generate_fake_embedding(query)

    # Filtered search (traced)
    cur.execute("""
        SELECT id, text, category, embedding <=> %s AS distance
        FROM documents
        WHERE category = %s
        ORDER BY embedding <=> %s
        LIMIT %s
    """, (query_vector, category, query_vector, limit))

    results = [
        {"id": row[0], "text": row[1], "category": row[2], "distance": row[3]}
        for row in cur.fetchall()
    ]

    cur.close()
    conn.close()
    return results


if __name__ == "__main__":
    # Setup database
    setup_database()

    # Example documents
    docs = [
        {"text": "Machine learning algorithms learn from data.", "category": "ml"},
        {"text": "Deep learning uses multi-layer neural networks.", "category": "dl"},
        {"text": "PostgreSQL is a powerful relational database.", "category": "db"},
        {"text": "Vector search enables semantic similarity.", "category": "db"},
        {"text": "Natural language processing analyzes text.", "category": "nlp"},
    ]

    # Add documents
    add_documents(docs)

    # L2 distance search
    query = "neural networks and deep learning"
    print(f"\nL2 distance search for: '{query}'")
    results = search_l2(query, limit=3)
    for r in results:
        print(f"  - [{r['category']}] {r['text'][:40]}... (distance: {r['distance']:.4f})")

    # Cosine similarity search
    print(f"\nCosine distance search:")
    cosine_results = search_cosine(query, limit=3)
    for r in cosine_results:
        print(f"  - [{r['category']}] {r['text'][:40]}... (distance: {r['distance']:.4f})")

    # Filtered search
    print(f"\nFiltered search (category='ml'):")
    filtered = search_with_filter(query, category="ml", limit=2)
    for r in filtered:
        print(f"  - {r['text'][:50]}...")
