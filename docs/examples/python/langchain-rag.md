# LangChain RAG Example

Build a traced Retrieval-Augmented Generation pipeline with LangChain.

## Prerequisites

```bash
pip install fi-instrumentation traceai-langchain traceai-openai traceai-chromadb
pip install langchain langchain-openai langchain-community chromadb
```

## Full Example

```python
import os
from fi_instrumentation import register, using_attributes
from fi_instrumentation.fi_types import ProjectType
from traceai_langchain import LangChainInstrumentor
from traceai_openai import OpenAIInstrumentor
from traceai_chromadb import ChromaDBInstrumentor

# LangChain imports
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

# 1. Set environment variables
os.environ["FI_API_KEY"] = "your-api-key"
os.environ["FI_SECRET_KEY"] = "your-secret-key"
os.environ["OPENAI_API_KEY"] = "your-openai-key"

# 2. Register and instrument
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="rag_pipeline"
)

LangChainInstrumentor().instrument(tracer_provider=trace_provider)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

# 3. Create components
embeddings = OpenAIEmbeddings()
llm = ChatOpenAI(model="gpt-4", temperature=0)

# 4. Sample documents
documents = [
    "Paris is the capital of France. It is known for the Eiffel Tower.",
    "London is the capital of England. Big Ben is a famous landmark.",
    "Tokyo is the capital of Japan. It is known for its cherry blossoms.",
    "Berlin is the capital of Germany. The Brandenburg Gate is iconic.",
]

# 5. Create vector store
vectorstore = Chroma.from_texts(
    texts=documents,
    embedding=embeddings,
    collection_name="capitals"
)
retriever = vectorstore.as_retriever(search_kwargs={"k": 2})

# 6. Create RAG prompt
template = """Answer the question based only on the following context:

Context:
{context}

Question: {question}

Answer:"""

prompt = ChatPromptTemplate.from_template(template)

# 7. Build RAG chain
def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

rag_chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | prompt
    | llm
    | StrOutputParser()
)

# 8. Query function
def ask(question: str, session_id: str = None) -> str:
    """Ask a question to the RAG pipeline."""
    with using_attributes(
        session_id=session_id,
        metadata={"pipeline": "rag", "version": "1.0"}
    ):
        return rag_chain.invoke(question)

# 9. Use it
if __name__ == "__main__":
    questions = [
        "What is the capital of France?",
        "Tell me about Tokyo's famous features.",
        "What landmark is Berlin known for?"
    ]

    for q in questions:
        print(f"Q: {q}")
        answer = ask(q, session_id="rag-session-001")
        print(f"A: {answer}\n")
```

## Trace Structure

The RAG pipeline creates a hierarchical trace:

```
rag_chain (CHAIN)
├── retriever (RETRIEVER)
│   └── embeddings.embed_query (EMBEDDING)
├── format_docs
├── prompt (CHAIN)
└── llm (LLM)
```

## Captured Attributes

### Retriever Span
| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `RETRIEVER` |
| `retrieval.documents` | Retrieved document contents |
| `input.value` | Query text |

### Embedding Span
| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `EMBEDDING` |
| `embedding.model_name` | `text-embedding-ada-002` |
| `embedding.embeddings` | Vector values (if not hidden) |

### LLM Span
| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `LLM` |
| `llm.model_name` | `gpt-4` |
| `llm.input_messages` | Formatted prompt |
| `llm.output_messages` | Model response |
| `llm.token_count.*` | Token usage |

## With Conversation Memory

```python
from langchain.memory import ConversationBufferMemory
from langchain.chains import ConversationalRetrievalChain

memory = ConversationBufferMemory(
    memory_key="chat_history",
    return_messages=True
)

conversational_chain = ConversationalRetrievalChain.from_llm(
    llm=llm,
    retriever=retriever,
    memory=memory,
    return_source_documents=True
)

def chat(question: str, session_id: str) -> str:
    with using_attributes(session_id=session_id):
        result = conversational_chain({"question": question})
        return result["answer"]

# Multi-turn conversation
session = "conv-001"
print(chat("What is the capital of France?", session))
print(chat("What is it famous for?", session))  # Uses context
```

## With Streaming

```python
from langchain_core.runnables import RunnableConfig

def ask_stream(question: str) -> str:
    """Stream the RAG response."""
    full_response = ""

    for chunk in rag_chain.stream(question):
        print(chunk, end="", flush=True)
        full_response += chunk

    print()
    return full_response

answer = ask_stream("Tell me about Paris.")
```

## With Experiments

Evaluate RAG quality:

```python
from fi_instrumentation.fi_types import (
    EvalTag, EvalTagType, EvalSpanKind, EvalName, ModelChoices
)

eval_tags = [
    # Evaluate retriever relevance
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.RETRIEVER,
        eval_name=EvalName.CONTEXT_RELEVANCE,
        custom_eval_name="retriever_relevance",
        mapping={
            "context": "raw.output",
            "query": "raw.input"
        },
        model=ModelChoices.TURING_SMALL
    ),
    # Evaluate LLM groundedness
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.GROUNDEDNESS,
        custom_eval_name="response_groundedness",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_SMALL
    ),
    # Check for hallucinations
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.DETECT_HALLUCINATION,
        custom_eval_name="hallucination_check",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_LARGE
    )
]

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="rag_quality_test",
    project_version_name="v1.0",
    eval_tags=eval_tags
)
```

## Different Vector Stores

### Pinecone

```python
from traceai_pinecone import PineconeInstrumentor
from langchain_pinecone import PineconeVectorStore

PineconeInstrumentor().instrument(tracer_provider=trace_provider)

vectorstore = PineconeVectorStore.from_texts(
    texts=documents,
    embedding=embeddings,
    index_name="my-index"
)
```

### Qdrant

```python
from traceai_qdrant import QdrantInstrumentor
from langchain_community.vectorstores import Qdrant

QdrantInstrumentor().instrument(tracer_provider=trace_provider)

vectorstore = Qdrant.from_texts(
    texts=documents,
    embedding=embeddings,
    collection_name="my-collection"
)
```

## Error Handling

```python
def safe_ask(question: str) -> str:
    try:
        return ask(question)
    except Exception as e:
        # Errors are captured in the trace
        print(f"RAG error: {e}")
        return "Sorry, I couldn't process your question."
```

## Related

- [Basic OpenAI](basic-openai.md)
- [Multi-Agent CrewAI](multi-agent-crewai.md)
- [Evaluation Tags](../../configuration/eval-tags.md)
