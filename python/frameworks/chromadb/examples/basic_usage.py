"""
Basic ChromaDB usage example with tracing.
"""

import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_chromadb import ChromaDBInstrumentor


def main():
    # Set up environment variables
    os.environ.setdefault("FI_API_KEY", "<your-api-key>")
    os.environ.setdefault("FI_SECRET_KEY", "<your-secret-key>")

    # Register tracer provider
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="chromadb-example"
    )

    # Instrument ChromaDB
    ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use ChromaDB as normal
    import chromadb

    # Create a client (in-memory for this example)
    client = chromadb.Client()

    # Create a collection
    collection = client.create_collection("documents")

    # Add some documents - this will be traced
    collection.add(
        ids=["doc1", "doc2", "doc3"],
        documents=[
            "The quick brown fox jumps over the lazy dog",
            "Machine learning is a subset of artificial intelligence",
            "Vector databases enable semantic search capabilities"
        ],
        metadatas=[
            {"source": "example", "category": "animals"},
            {"source": "example", "category": "technology"},
            {"source": "example", "category": "technology"}
        ]
    )

    print(f"Collection count: {collection.count()}")

    # Query the collection - this will be traced
    results = collection.query(
        query_texts=["What is machine learning?"],
        n_results=2
    )

    print("\nQuery Results:")
    for i, (doc_id, document, distance) in enumerate(zip(
        results["ids"][0],
        results["documents"][0],
        results["distances"][0]
    )):
        print(f"  {i+1}. ID: {doc_id}, Distance: {distance:.4f}")
        print(f"     Document: {document[:50]}...")

    # Get specific documents - this will be traced
    specific = collection.get(ids=["doc1"])
    print(f"\nFetched document: {specific['documents'][0][:50]}...")

    # Delete a document - this will be traced
    collection.delete(ids=["doc3"])
    print(f"\nAfter deletion, count: {collection.count()}")


if __name__ == "__main__":
    main()
