# traceAI-chromadb

OpenTelemetry instrumentation for [ChromaDB](https://www.trychroma.com/) vector database.

## Installation

```bash
pip install traceAI-chromadb
```

## Quick Start

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_chromadb import ChromaDBInstrumentor
import chromadb

# Set up environment
os.environ["FI_API_KEY"] = "<your-api-key>"
os.environ["FI_SECRET_KEY"] = "<your-secret-key>"

# Register tracer
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-rag-app"
)

# Instrument ChromaDB
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

# Use ChromaDB as normal - all operations are traced!
client = chromadb.Client()
collection = client.create_collection("my-collection")

# Add documents
collection.add(
    ids=["id1", "id2"],
    documents=["Hello world", "Goodbye world"],
    metadatas=[{"source": "doc1"}, {"source": "doc2"}]
)

# Query
results = collection.query(
    query_texts=["Hello"],
    n_results=5
)
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `add` | `chroma add` | Add documents/embeddings |
| `query` | `chroma query` | Semantic search |
| `get` | `chroma get` | Retrieve by ID or filter |
| `update` | `chroma update` | Update existing documents |
| `upsert` | `chroma upsert` | Insert or update |
| `delete` | `chroma delete` | Delete documents |
| `count` | `chroma count` | Get collection count |
| `peek` | `chroma peek` | Preview collection |

## Span Attributes

### Common Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.system` | string | Always `"chroma"` |
| `db.operation.name` | string | Operation name |
| `db.namespace` | string | Collection name |
| `db.vector.collection.name` | string | Collection name |

### Query Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.vector.query.top_k` | int | n_results parameter |
| `db.vector.query.filter` | string | JSON where clause |
| `db.vector.query.where_document` | string | Document filter |
| `db.vector.query.include` | string | Included fields |
| `db.vector.query.type` | string | "embedding" or "text" |
| `db.vector.results.count` | int | Number of results |
| `db.vector.results.ids` | string | JSON result IDs |
| `db.vector.results.scores` | string | JSON distances |

### Add/Upsert Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `db.vector.upsert.count` | int | Number of items |
| `db.vector.upsert.dimensions` | int | Embedding dimensions |
| `db.vector.documents.count` | int | Number of documents |

## Examples

### Persistent Client

```python
from fi_instrumentation import register
from traceai_chromadb import ChromaDBInstrumentor
import chromadb

# Register and instrument
trace_provider = register(project_name="chroma-persistent")
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

# Use persistent client
client = chromadb.PersistentClient(path="/path/to/db")
collection = client.get_or_create_collection("documents")

# All operations are traced
collection.add(
    ids=["doc1"],
    documents=["Important document content"],
    embeddings=[[0.1] * 384]  # Your embeddings
)
```

### With Embedding Function

```python
from chromadb.utils import embedding_functions

# Use with OpenAI embeddings
openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key="sk-...",
    model_name="text-embedding-3-small"
)

collection = client.create_collection(
    name="openai-docs",
    embedding_function=openai_ef
)

# Add documents - embeddings generated automatically
collection.add(
    ids=["id1", "id2"],
    documents=["Document one", "Document two"]
)

# Query with text - embedding generated automatically
results = collection.query(
    query_texts=["search query"],
    n_results=5
)
```

### RAG with LangChain

```python
from fi_instrumentation import register
from traceai_chromadb import ChromaDBInstrumentor
from traceai_langchain import LangChainInstrumentor

# Instrument both
trace_provider = register(project_name="rag-langchain")
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)
LangChainInstrumentor().instrument(tracer_provider=trace_provider)

# Use LangChain with Chroma
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

vectorstore = Chroma(
    collection_name="langchain-docs",
    embedding_function=OpenAIEmbeddings()
)

# Add documents
vectorstore.add_texts(
    texts=["Document 1", "Document 2"],
    metadatas=[{"source": "web"}, {"source": "pdf"}]
)

# Search - all operations traced
docs = vectorstore.similarity_search("query", k=5)
```

## Testing

ChromaDB runs in-memory, so no external database is needed for testing.

### Unit Tests

```bash
cd python/frameworks/chromadb
pip install pytest chromadb
pytest tests/ -v
```

### E2E Integration Tests

```bash
cd python/frameworks/tests_e2e
pytest test_chromadb_e2e.py -v
```

### Example

Run the semantic search example:

```bash
cd examples
python semantic_search.py
```

## Resources

- [ChromaDB Documentation](https://docs.trychroma.com/)
- [TraceAI Documentation](https://docs.futureagi.com/)
- [OpenTelemetry Database Conventions](https://opentelemetry.io/docs/specs/semconv/database/)
- [Vector DB Guide](../../docs/VECTOR_DB_GUIDE.md)

## License

Apache License 2.0
