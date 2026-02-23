"""AutoGen v0.4 RAG pipeline example with tracing.

This example demonstrates:
- Building a RAG (Retrieval-Augmented Generation) pipeline
- Using tools for document retrieval
- Tracing the full RAG workflow
"""

import asyncio
import os
import json
from typing import Annotated, List

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-rag-pipeline",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient


# Simulated document store
DOCUMENTS = {
    "doc1": {
        "title": "Python Best Practices",
        "content": """Python best practices include using virtual environments,
        following PEP 8 style guide, writing docstrings, using type hints,
        handling exceptions properly, and writing unit tests. Always use
        meaningful variable names and keep functions small and focused.""",
        "tags": ["python", "coding", "best-practices"],
    },
    "doc2": {
        "title": "Machine Learning Fundamentals",
        "content": """Machine learning involves training models on data to make
        predictions. Key concepts include supervised learning (classification,
        regression), unsupervised learning (clustering, dimensionality reduction),
        and reinforcement learning. Data preprocessing and feature engineering
        are crucial for model performance.""",
        "tags": ["ml", "ai", "data-science"],
    },
    "doc3": {
        "title": "API Design Guidelines",
        "content": """Good API design follows REST principles: use HTTP methods
        correctly (GET, POST, PUT, DELETE), use meaningful resource names,
        version your API, return appropriate status codes, and provide clear
        error messages. Consider pagination for large collections and use
        consistent naming conventions.""",
        "tags": ["api", "rest", "web-development"],
    },
    "doc4": {
        "title": "Database Optimization",
        "content": """Database optimization includes proper indexing, query
        optimization, connection pooling, and caching strategies. Use EXPLAIN
        to analyze query plans. Consider denormalization for read-heavy workloads
        and use appropriate data types. Regular maintenance like VACUUM and
        ANALYZE keeps performance optimal.""",
        "tags": ["database", "performance", "sql"],
    },
}


def search_documents(
    query: Annotated[str, "Search query to find relevant documents"],
    max_results: Annotated[int, "Maximum number of documents to return"] = 3,
) -> str:
    """Search the document store for relevant documents."""
    # Simple keyword-based search (in production, use embeddings/vector search)
    query_terms = query.lower().split()
    results = []

    for doc_id, doc in DOCUMENTS.items():
        score = 0
        text = f"{doc['title']} {doc['content']} {' '.join(doc['tags'])}".lower()

        for term in query_terms:
            if term in text:
                score += text.count(term)

        if score > 0:
            results.append(
                {
                    "doc_id": doc_id,
                    "title": doc["title"],
                    "score": score,
                    "preview": doc["content"][:150] + "...",
                }
            )

    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)

    return json.dumps(
        {
            "query": query,
            "results": results[:max_results],
            "total_found": len(results),
        },
        indent=2,
    )


def get_document(
    doc_id: Annotated[str, "Document ID to retrieve"],
) -> str:
    """Retrieve the full content of a specific document."""
    if doc_id in DOCUMENTS:
        doc = DOCUMENTS[doc_id]
        return json.dumps(
            {
                "doc_id": doc_id,
                "title": doc["title"],
                "content": doc["content"],
                "tags": doc["tags"],
            },
            indent=2,
        )
    else:
        return json.dumps({"error": f"Document {doc_id} not found"})


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Create RAG agent with retrieval tools
    rag_agent = AssistantAgent(
        name="rag_assistant",
        model_client=model,
        tools=[search_documents, get_document],
        system_message="""You are a knowledgeable assistant with access to a document store.

        When answering questions:
        1. First search for relevant documents using the search_documents tool
        2. Retrieve full documents using get_document for the most relevant results
        3. Synthesize information from the documents to answer the question
        4. Cite which documents you used in your answer

        Always ground your answers in the retrieved documents.""",
    )

    # Test RAG queries
    queries = [
        "What are the best practices for writing Python code?",
        "How do I optimize database performance?",
        "What should I consider when designing a REST API?",
        "Can you explain machine learning basics?",
    ]

    for query in queries:
        print(f"\n{'=' * 60}")
        print(f"Question: {query}")
        print("-" * 60)

        response = await rag_agent.on_messages(
            messages=[TextMessage(content=query, source="user")],
            cancellation_token=None,
        )

        print(f"\nAnswer:\n{response.chat_message.content}")

        # Show retrieved documents
        if hasattr(response, "inner_messages") and response.inner_messages:
            tool_calls = [
                msg
                for msg in response.inner_messages
                if "tool" in type(msg).__name__.lower()
            ]
            if tool_calls:
                print(f"\n[Used {len(tool_calls)} tool calls for retrieval]")


if __name__ == "__main__":
    asyncio.run(main())
