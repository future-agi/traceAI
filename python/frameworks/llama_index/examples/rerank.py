import os

import requests
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from llama_index.core import Document, VectorStoreIndex
from llama_index.postprocessor.cohere_rerank import CohereRerank
from traceai_llamaindex import LlamaIndexInstrumentor

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)
# Initialize the Llama Index instrumentor
LlamaIndexInstrumentor().instrument(tracer_provider=trace_provider)


def pprint_response(response, show_source=True):
    """Pretty print the response and sources."""
    print("Final Response:", response.response)
    if show_source:
        print("_" * 70)
        for idx, source_node in enumerate(response.source_nodes):
            print(f"Source Node {idx + 1}/{len(response.source_nodes)}")
            print(f"Node ID: {source_node.node.node_id}")
            print(f"Similarity: {source_node.score:.8f}")
            print(f"Text: {source_node.node.text}")
            print("_" * 70)


def main():
    # Load document directly from URL using requests
    url = "https://raw.githubusercontent.com/run-llama/llama_index/main/docs/docs/examples/data/paul_graham/paul_graham_essay.txt"
    response = requests.get(url)
    text_content = response.text

    # Create a Document object
    documents = [Document(text=text_content)]

    # Build index
    index = VectorStoreIndex.from_documents(documents=documents)

    # Example 1: With Cohere Rerank
    print("\nExample 1: Using Cohere Rerank")
    api_key = os.environ["COHERE_API_KEY"]
    cohere_rerank = CohereRerank(api_key=api_key, top_n=2)
    query_engine_rerank = index.as_query_engine(
        similarity_top_k=10,
        node_postprocessors=[cohere_rerank],
    )
    response_rerank = query_engine_rerank.query(
        "What did Sam Altman do in this essay?",
    )
    pprint_response(response_rerank, show_source=True)

    # Example 2: Without Cohere Rerank
    print("\nExample 2: Direct Retrieval without Rerank")
    query_engine_direct = index.as_query_engine(
        similarity_top_k=2,
    )
    response_direct = query_engine_direct.query(
        "What did Sam Altman do in this essay?",
    )
    pprint_response(response_direct, show_source=True)


if __name__ == "__main__":
    main()
