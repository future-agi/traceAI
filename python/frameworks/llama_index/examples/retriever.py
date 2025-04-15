from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from llama_index import SimpleDirectoryReader, VectorStoreIndex
from llama_index.postprocessor import SimilarityPostprocessor
from llama_index.query_engine import RetrieverQueryEngine
from llama_index.retrievers import VectorIndexRetriever
from traceai_llamaindex import LlamaIndexInstrumentor

eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the Llama Index instrumentor
LlamaIndexInstrumentor().instrument(tracer_provider=trace_provider)


def create_index(documents_path):
    # Load documents from the specified directory
    documents = SimpleDirectoryReader(documents_path).load_data()

    # Create a vector store index
    index = VectorStoreIndex.from_documents(documents)
    return index


def setup_retriever_engine(index, similarity_cutoff=0.7, top_k=10):
    # Configure the retriever
    retriever = VectorIndexRetriever(index=index, similarity_top_k=top_k)

    # Create a similarity postprocessor for reranking
    postprocessor = SimilarityPostprocessor(similarity_cutoff=similarity_cutoff)

    # Create the query engine with the retriever and postprocessor
    query_engine = RetrieverQueryEngine(
        retriever=retriever, node_postprocessors=[postprocessor]
    )

    return query_engine


def search_and_rerank(query_engine, query_text):
    # Perform the search and reranking
    response = query_engine.query(query_text)

    # Get source nodes (retrieved documents)
    source_nodes = response.source_nodes

    # Print results
    print(f"\nQuery: {query_text}")
    print("\nRanked Results:")
    for idx, node in enumerate(source_nodes, 1):
        print(f"\n{idx}. Score: {node.score:.4f}")
        print(f"Text: {node.node.text[:200]}...")  # Show first 200 chars


def main():
    # Initialize the system
    documents_path = "path/to/your/documents"
    index = create_index(documents_path)
    query_engine = setup_retriever_engine(index, similarity_cutoff=0.7, top_k=5)

    # Example queries
    queries = [
        "What is machine learning?",
        "How does natural language processing work?",
    ]

    # Process each query
    for query in queries:
        search_and_rerank(query_engine, query)


if __name__ == "__main__":
    main()
