"""RAG Pipeline example with Pydantic AI and tracing.

This example demonstrates a Retrieval Augmented Generation (RAG) pipeline
with full OpenTelemetry tracing for each step.
"""

from typing import List
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from pydantic import BaseModel, Field

from traceai_pydantic_ai import PydanticAIInstrumentor


class Document(BaseModel):
    """A retrieved document."""

    title: str
    content: str
    relevance_score: float


class RAGResponse(BaseModel):
    """Structured response from RAG pipeline."""

    answer: str = Field(description="The answer to the user's question")
    sources: List[str] = Field(description="List of source documents used")
    confidence: float = Field(description="Confidence score 0-1", ge=0, le=1)


def setup_tracing():
    """Setup OpenTelemetry with console exporter for demo."""
    provider = TracerProvider()
    processor = SimpleSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider


# Simulated document database
KNOWLEDGE_BASE = [
    Document(
        title="Python Best Practices",
        content="Use type hints for better code quality. Follow PEP 8 style guide.",
        relevance_score=0.95,
    ),
    Document(
        title="Testing in Python",
        content="Use pytest for testing. Write unit tests and integration tests.",
        relevance_score=0.88,
    ),
    Document(
        title="Python Performance",
        content="Use generators for memory efficiency. Profile before optimizing.",
        relevance_score=0.82,
    ),
]


def main():
    # Setup tracing
    provider = setup_tracing()

    # Initialize Pydantic AI instrumentation
    PydanticAIInstrumentor().instrument(tracer_provider=provider)

    # Import after instrumentation
    from pydantic_ai import Agent, RunContext

    # Create RAG agent
    agent = Agent(
        "openai:gpt-4o-mini",
        result_type=RAGResponse,
        instructions=(
            "You are a helpful assistant that answers questions based on "
            "provided documents. Always cite your sources."
        ),
    )

    # Retrieval tool
    @agent.tool
    def search_documents(ctx: RunContext, query: str) -> str:
        """Search the knowledge base for relevant documents.

        Args:
            query: The search query.

        Returns:
            Formatted string with relevant documents.
        """
        # In production, this would call a vector database
        # For demo, return simulated results
        relevant_docs = [
            doc for doc in KNOWLEDGE_BASE if doc.relevance_score > 0.8
        ]

        result = "Retrieved Documents:\n\n"
        for doc in relevant_docs:
            result += f"### {doc.title} (score: {doc.relevance_score})\n"
            result += f"{doc.content}\n\n"

        return result

    # Run RAG query
    print("Running RAG pipeline...")
    result = agent.run_sync(
        "What are some Python best practices for writing clean code?"
    )

    response = result.output
    print(f"\nAnswer: {response.answer}")
    print(f"\nSources: {response.sources}")
    print(f"Confidence: {response.confidence}")
    print(f"\nUsage: {result.usage}")


if __name__ == "__main__":
    main()
