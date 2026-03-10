"""Cohere rerank example for RAG pipelines."""
import cohere
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_cohere import CohereInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument Cohere
CohereInstrumentor().instrument(tracer_provider=provider)


def main():
    client = cohere.Client()

    # Documents to search through
    documents = [
        "Paris is the capital and most populous city of France.",
        "London is the capital of England and the United Kingdom.",
        "The Eiffel Tower is a famous landmark located in Paris, France.",
        "France is a country in Western Europe known for its cuisine and culture.",
        "The Louvre Museum in Paris houses the Mona Lisa painting.",
        "Berlin is the capital of Germany.",
    ]

    query = "What is the capital of France?"

    print(f"Query: {query}\n")
    print("=== Reranking Documents ===")

    # Rerank documents
    response = client.rerank(
        model="rerank-english-v3.0",
        query=query,
        documents=documents,
        top_n=3,
    )

    print("Top results:")
    for result in response.results:
        print(f"  Score: {result.relevance_score:.4f}")
        print(f"  Document: {documents[result.index]}\n")

    # Use top results for RAG
    print("=== RAG with Top Results ===")
    top_docs = [{"text": documents[r.index]} for r in response.results]

    rag_response = client.chat(
        model="command-r-plus",
        message=query,
        documents=top_docs,
    )
    print(f"Answer: {rag_response.text}")

    if rag_response.citations:
        print("\nCitations:")
        for citation in rag_response.citations:
            print(f"  - {citation}")


if __name__ == "__main__":
    main()
