"""
MongoDB Atlas Vector Search Example

This example demonstrates vector search using MongoDB Atlas with tracing.

Prerequisites:
    pip install traceai-mongodb pymongo

Environment variables:
    MONGODB_URI: Your MongoDB Atlas connection string
    FI_API_KEY: Your Future AGI API key (optional)

Note: Requires MongoDB Atlas cluster with Vector Search index configured.
"""

import os
from pymongo import MongoClient
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_mongodb import MongoDBInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="mongodb-atlas-vector-search"
)

# Instrument MongoDB
MongoDBInstrumentor().instrument(tracer_provider=trace_provider)

# Connect to MongoDB Atlas
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
client = MongoClient(MONGODB_URI)

DB_NAME = "vector_demo"
COLLECTION_NAME = "documents"
DIMENSION = 384

db = client[DB_NAME]
collection = db[COLLECTION_NAME]


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
    """Add documents with embeddings."""
    docs_to_insert = []
    for doc in documents:
        docs_to_insert.append({
            "text": doc["text"],
            "category": doc.get("category", "general"),
            "embedding": generate_fake_embedding(doc["text"]),
        })

    # Insert many (traced)
    result = collection.insert_many(docs_to_insert)
    print(f"Inserted {len(result.inserted_ids)} documents")


def vector_search(query: str, limit: int = 5, num_candidates: int = 100) -> list[dict]:
    """
    Perform vector search using MongoDB Atlas $vectorSearch.

    Note: Requires a vector search index named 'vector_index' on the 'embedding' field.
    """
    query_vector = generate_fake_embedding(query)

    # Vector search aggregation (traced)
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": num_candidates,
                "limit": limit,
            }
        },
        {
            "$project": {
                "text": 1,
                "category": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        }
    ]

    results = list(collection.aggregate(pipeline))

    return [
        {
            "id": str(r["_id"]),
            "text": r["text"],
            "category": r["category"],
            "score": r.get("score", 0),
        }
        for r in results
    ]


def vector_search_with_filter(
    query: str,
    category: str,
    limit: int = 5
) -> list[dict]:
    """Vector search with pre-filter."""
    query_vector = generate_fake_embedding(query)

    # Vector search with filter (traced)
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": 100,
                "limit": limit,
                "filter": {"category": category},
            }
        },
        {
            "$project": {
                "text": 1,
                "category": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        }
    ]

    results = list(collection.aggregate(pipeline))

    return [
        {
            "id": str(r["_id"]),
            "text": r["text"],
            "score": r.get("score", 0),
        }
        for r in results
    ]


def find_documents(filter_query: dict = None, limit: int = 10) -> list[dict]:
    """Find documents using standard MongoDB query."""
    # Find (traced)
    cursor = collection.find(filter_query or {}).limit(limit)

    return [
        {
            "id": str(doc["_id"]),
            "text": doc["text"],
            "category": doc.get("category", ""),
        }
        for doc in cursor
    ]


def update_document(doc_id: str, updates: dict):
    """Update a document."""
    from bson import ObjectId

    # Update one (traced)
    result = collection.update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": updates}
    )
    print(f"Modified {result.modified_count} document(s)")


def delete_by_category(category: str):
    """Delete documents by category."""
    # Delete many (traced)
    result = collection.delete_many({"category": category})
    print(f"Deleted {result.deleted_count} documents")


if __name__ == "__main__":
    # Clear existing data
    collection.delete_many({})

    # Example documents
    docs = [
        {"text": "Machine learning enables pattern recognition.", "category": "ml"},
        {"text": "Deep learning uses multi-layer neural networks.", "category": "dl"},
        {"text": "MongoDB is a document database.", "category": "db"},
        {"text": "Vector search finds semantically similar documents.", "category": "db"},
        {"text": "Natural language processing analyzes text.", "category": "nlp"},
    ]

    # Add documents
    add_documents(docs)

    # Standard find
    print(f"\nAll documents:")
    all_docs = find_documents(limit=5)
    for d in all_docs:
        print(f"  - [{d['category']}] {d['text'][:40]}...")

    # Vector search (requires Atlas with vector index)
    # Uncomment if you have Atlas with vector search configured:
    #
    # query = "neural networks and AI"
    # print(f"\nVector search for: '{query}'")
    # results = vector_search(query, limit=3)
    # for r in results:
    #     print(f"  - [{r['category']}] {r['text'][:40]}... (score: {r['score']:.4f})")
    #
    # print(f"\nFiltered vector search (category='ml'):")
    # filtered = vector_search_with_filter(query, category="ml", limit=2)
    # for r in filtered:
    #     print(f"  - {r['text'][:50]}...")

    print("\nNote: Vector search requires MongoDB Atlas with a vector search index.")
    print("See: https://www.mongodb.com/docs/atlas/atlas-vector-search/")
