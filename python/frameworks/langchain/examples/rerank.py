import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from langchain.embeddings import OpenAIEmbeddings
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import CohereRerank
from langchain.schema import Document
from langchain.text_splitter import CharacterTextSplitter
from langchain.vectorstores import FAISS
from traceai_langchain import LangChainInstrumentor

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="SQL_AGENT",
    project_version_name="V1",
)

LangChainInstrumentor().instrument(tracer_provider=trace_provider)

sample_docs = [
    Document(
        page_content="Artificial Intelligence (AI) is the simulation of human intelligence by machines. "
        "It includes machine learning, natural language processing, and robotics. "
        "AI systems can learn from experience, adjust to new inputs, and perform human-like tasks.",
        metadata={"source": "AI Introduction"},
    ),
    Document(
        page_content="Machine Learning is a subset of AI that enables systems to learn and improve "
        "from experience without being explicitly programmed. Popular ML algorithms include "
        "neural networks, decision trees, and random forests.",
        metadata={"source": "ML Basics"},
    ),
    Document(
        page_content="Python is a high-level programming language known for its simplicity and readability. "
        "It's widely used in data science, web development, and automation. Python has a rich ecosystem "
        "of libraries and frameworks.",
        metadata={"source": "Python Guide"},
    ),
    Document(
        page_content="Deep Learning is a type of machine learning based on artificial neural networks. "
        "It's particularly effective in tasks like image recognition, natural language processing, "
        "and speech recognition.",
        metadata={"source": "Deep Learning Info"},
    ),
    Document(
        page_content="Data Science combines statistics, programming, and domain expertise to extract "
        "meaningful insights from data. It involves data collection, cleaning, analysis, and visualization. "
        "Python is a popular language for data science.",
        metadata={"source": "Data Science Overview"},
    ),
]


def create_vector_store(documents):
    """Create a FAISS vector store from documents."""
    embeddings = OpenAIEmbeddings()
    vector_store = FAISS.from_documents(documents, embeddings)
    return vector_store


def setup_reranker():
    """Setup the Cohere reranker."""
    compressor = CohereRerank(
        model="rerank-english-v2.0", top_n=3  # Reduced to 3 for this example
    )
    return compressor


def main():
    # Create vector store directly from sample documents
    vector_store = create_vector_store(sample_docs)
    base_retriever = vector_store.as_retriever(search_kwargs={"k": 5})

    # Setup reranker
    compressor = setup_reranker()

    # Create compression retriever
    compression_retriever = ContextualCompressionRetriever(
        base_compressor=compressor, base_retriever=base_retriever
    )

    # Example queries
    queries = [
        "What is artificial intelligence?",
        "How is Python used in data science?",
        "Explain machine learning",
    ]

    # Get and print results for each query
    for query in queries:
        # print(f"\nQuery: {query}")
        # print("\nReranked Results:")
        compressed_docs = compression_retriever.get_relevant_documents(query)
        for i, doc in enumerate(compressed_docs, 1):
            print(f"\n{i}. Source: {doc.metadata['source']}")
            print(f"   Content: {doc.page_content}")
        print("\n" + "=" * 50)


if __name__ == "__main__":
    main()
