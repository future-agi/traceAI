"""
RAG (Retrieval-Augmented Generation) pipeline example with full tracing.

This example shows how to trace both the retrieval (Pinecone) and
generation (OpenAI) steps of a RAG pipeline.
"""

import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_pinecone import PineconeInstrumentor
from traceai_openai import OpenAIInstrumentor


def setup_tracing():
    """Set up tracing for the RAG pipeline."""
    os.environ.setdefault("FI_API_KEY", "<your-api-key>")
    os.environ.setdefault("FI_SECRET_KEY", "<your-secret-key>")

    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="rag-pipeline"
    )

    # Instrument both Pinecone and OpenAI
    PineconeInstrumentor().instrument(tracer_provider=trace_provider)
    OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

    return trace_provider


def rag_query(question: str, index, client) -> str:
    """
    Execute a RAG query with full tracing.

    Args:
        question: User question
        index: Pinecone index
        client: OpenAI client

    Returns:
        Generated answer
    """
    # Step 1: Generate embedding for the question
    embedding_response = client.embeddings.create(
        model="text-embedding-3-small",
        input=question
    )
    query_embedding = embedding_response.data[0].embedding

    # Step 2: Search Pinecone for relevant documents
    search_results = index.query(
        vector=query_embedding,
        top_k=5,
        include_metadata=True
    )

    # Step 3: Build context from retrieved documents
    context_parts = []
    for i, match in enumerate(search_results.matches, 1):
        if match.metadata:
            text = match.metadata.get("text", "")
            source = match.metadata.get("source", "unknown")
            context_parts.append(f"[{i}] {text} (Source: {source})")

    context = "\n\n".join(context_parts)

    # Step 4: Generate answer using retrieved context
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {
                "role": "system",
                "content": f"""You are a helpful assistant. Answer the user's question
based on the following context. If the context doesn't contain
relevant information, say so.

Context:
{context}"""
            },
            {
                "role": "user",
                "content": question
            }
        ],
        temperature=0.7
    )

    return response.choices[0].message.content


def main():
    # Set up tracing
    setup_tracing()

    # Initialize clients
    import pinecone
    import openai

    pc = pinecone.Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
    index = pc.Index("documents")
    client = openai.OpenAI()

    # Ask a question
    question = "What is retrieval augmented generation and how does it work?"
    print(f"Question: {question}\n")

    answer = rag_query(question, index, client)
    print(f"Answer: {answer}")


if __name__ == "__main__":
    main()
