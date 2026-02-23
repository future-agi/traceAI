"""
Semantic Search Example with Weaviate v4

This example demonstrates semantic search using Weaviate v4 API with tracing.

Prerequisites:
    pip install traceai-weaviate weaviate-client>=4.0.0

Environment variables:
    WEAVIATE_URL: Your Weaviate server URL
    WEAVIATE_API_KEY: Your Weaviate API key (if using cloud)
    OPENAI_API_KEY: For text2vec-openai vectorizer
    FI_API_KEY: Your Future AGI API key (optional)
"""

import os
import weaviate
from weaviate.classes.config import Configure, Property, DataType
from weaviate.classes.query import MetadataQuery
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_weaviate import WeaviateInstrumentor

# Initialize tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="weaviate-semantic-search"
)

# Instrument Weaviate
WeaviateInstrumentor().instrument(tracer_provider=trace_provider)

# Connect to Weaviate
# For local: weaviate.connect_to_local()
# For cloud: weaviate.connect_to_wcs(cluster_url="...", auth_credentials=...)
client = weaviate.connect_to_local()

COLLECTION_NAME = "Document"


def setup_collection():
    """Create collection with text2vec vectorizer."""
    try:
        # Delete if exists (for demo)
        if client.collections.exists(COLLECTION_NAME):
            client.collections.delete(COLLECTION_NAME)

        # Create collection
        client.collections.create(
            name=COLLECTION_NAME,
            properties=[
                Property(name="title", data_type=DataType.TEXT),
                Property(name="content", data_type=DataType.TEXT),
                Property(name="category", data_type=DataType.TEXT),
            ],
            vectorizer_config=Configure.Vectorizer.text2vec_openai(),
        )
        print(f"Created collection: {COLLECTION_NAME}")
    except Exception as e:
        print(f"Collection setup error: {e}")


def add_documents(documents: list[dict]):
    """Add documents to Weaviate."""
    collection = client.collections.get(COLLECTION_NAME)

    # Insert documents (traced)
    with collection.batch.dynamic() as batch:
        for doc in documents:
            batch.add_object(
                properties={
                    "title": doc["title"],
                    "content": doc["content"],
                    "category": doc.get("category", "general"),
                }
            )

    print(f"Added {len(documents)} documents")


def near_text_search(query: str, limit: int = 5) -> list[dict]:
    """Search using natural language query."""
    collection = client.collections.get(COLLECTION_NAME)

    # Near text search (traced)
    response = collection.query.near_text(
        query=query,
        limit=limit,
        return_metadata=MetadataQuery(distance=True, certainty=True),
    )

    return [
        {
            "title": obj.properties.get("title", ""),
            "content": obj.properties.get("content", ""),
            "distance": obj.metadata.distance,
            "certainty": obj.metadata.certainty,
        }
        for obj in response.objects
    ]


def hybrid_search(query: str, alpha: float = 0.5, limit: int = 5) -> list[dict]:
    """Hybrid search combining vector and keyword search."""
    collection = client.collections.get(COLLECTION_NAME)

    # Hybrid search (traced)
    response = collection.query.hybrid(
        query=query,
        alpha=alpha,  # 0 = pure BM25, 1 = pure vector
        limit=limit,
        return_metadata=MetadataQuery(score=True),
    )

    return [
        {
            "title": obj.properties.get("title", ""),
            "content": obj.properties.get("content", ""),
            "score": obj.metadata.score,
        }
        for obj in response.objects
    ]


def bm25_search(query: str, limit: int = 5) -> list[dict]:
    """Keyword search using BM25."""
    collection = client.collections.get(COLLECTION_NAME)

    # BM25 search (traced)
    response = collection.query.bm25(
        query=query,
        limit=limit,
        return_metadata=MetadataQuery(score=True),
    )

    return [
        {
            "title": obj.properties.get("title", ""),
            "content": obj.properties.get("content", ""),
            "score": obj.metadata.score,
        }
        for obj in response.objects
    ]


def delete_by_category(category: str):
    """Delete documents by category."""
    from weaviate.classes.query import Filter

    collection = client.collections.get(COLLECTION_NAME)

    # Delete many (traced)
    result = collection.data.delete_many(
        where=Filter.by_property("category").equal(category)
    )

    print(f"Deleted {result.successful} documents in category '{category}'")


if __name__ == "__main__":
    try:
        # Setup
        setup_collection()

        # Example documents
        docs = [
            {"title": "ML Basics", "content": "Machine learning enables predictions from data.", "category": "ml"},
            {"title": "Deep Learning", "content": "Deep learning uses neural networks with many layers.", "category": "ml"},
            {"title": "NLP Guide", "content": "Natural language processing analyzes text data.", "category": "nlp"},
            {"title": "Computer Vision", "content": "Computer vision interprets images and video.", "category": "cv"},
        ]

        # Add documents
        add_documents(docs)

        # Near text search
        query = "neural networks and deep learning"
        print(f"\nNear text search for: '{query}'")
        results = near_text_search(query, limit=3)
        for r in results:
            print(f"  - {r['title']}: {r['content'][:40]}... (certainty: {r['certainty']:.3f})")

        # Hybrid search
        print(f"\nHybrid search (alpha=0.7):")
        hybrid_results = hybrid_search(query, alpha=0.7, limit=2)
        for r in hybrid_results:
            print(f"  - {r['title']}: score={r['score']:.3f}")

        # BM25 search
        print(f"\nBM25 keyword search for 'neural':")
        bm25_results = bm25_search("neural", limit=2)
        for r in bm25_results:
            print(f"  - {r['title']}: score={r['score']:.3f}")

    finally:
        client.close()
