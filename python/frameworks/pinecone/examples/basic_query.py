"""
Basic Pinecone query example with tracing.
"""

import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pinecone import PineconeInstrumentor


def main():
    # Set up environment variables
    os.environ.setdefault("FI_API_KEY", "<your-api-key>")
    os.environ.setdefault("FI_SECRET_KEY", "<your-secret-key>")

    # Register tracer provider
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="pinecone-example"
    )

    # Instrument Pinecone
    PineconeInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use Pinecone as normal
    import pinecone

    pc = pinecone.Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
    index = pc.Index("your-index-name")

    # Query vectors - this will be traced
    results = index.query(
        vector=[0.1] * 1536,  # Your query vector
        top_k=10,
        include_metadata=True,
        namespace="default"
    )

    print(f"Found {len(results.matches)} results")
    for match in results.matches:
        print(f"  ID: {match.id}, Score: {match.score}")


if __name__ == "__main__":
    main()
