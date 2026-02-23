# Vector Database Instrumentation Guide

TraceAI provides OpenTelemetry instrumentation for 9 major vector databases,
enabling full observability of RAG (Retrieval-Augmented Generation) pipelines.

## Supported Vector Databases

| Database | Package | Use Case | Best For |
|----------|---------|----------|----------|
| **Pinecone** | `traceAI-pinecone` | Managed cloud | Production RAG at scale |
| **ChromaDB** | `traceAI-chromadb` | Embedded/cloud | Development, prototyping |
| **Qdrant** | `traceAI-qdrant` | Self-hosted/cloud | High-performance search |
| **Weaviate** | `traceAI-weaviate` | Self-hosted/cloud | GraphQL, semantic search |
| **Milvus** | `traceAI-milvus` | Self-hosted | Large-scale deployments |
| **LanceDB** | `traceAI-lancedb` | Embedded | Local dev, edge, serverless |
| **MongoDB** | `traceAI-mongodb` | Atlas Vector Search | MongoDB ecosystem |
| **pgvector** | `traceAI-pgvector` | PostgreSQL extension | Existing Postgres users |
| **Redis** | `traceAI-redis` | RediSearch | Low-latency, caching |

## Quick Start

### Installation

```bash
# Install the vector DB package you need
pip install traceAI-pinecone    # For Pinecone
pip install traceAI-chromadb    # For ChromaDB
pip install traceAI-qdrant      # For Qdrant
pip install traceAI-weaviate    # For Weaviate
pip install traceAI-milvus      # For Milvus
pip install traceAI-lancedb     # For LanceDB
pip install traceAI-mongodb     # For MongoDB Atlas Vector
pip install traceAI-pgvector    # For pgvector
pip install traceAI-redis       # For Redis Vector
```

### Basic Usage

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

# Import the instrumentor for your database
from traceai_chromadb import ChromaDBInstrumentor  # Example: ChromaDB

# Register tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-rag-app"
)

# Instrument the database
ChromaDBInstrumentor().instrument(tracer_provider=trace_provider)

# Use your database normally - all operations are traced!
import chromadb
client = chromadb.Client()
collection = client.create_collection("docs")
collection.add(ids=["1"], documents=["Hello world"])
results = collection.query(query_texts=["greeting"], n_results=5)
```

## Traced Operations by Database

### Pinecone

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `query` | `pinecone query` | top_k, namespace, filter, include_metadata |
| `upsert` | `pinecone upsert` | upsert_count, namespace |
| `delete` | `pinecone delete` | delete_count, namespace |
| `fetch` | `pinecone fetch` | ids_count, namespace |
| `update` | `pinecone update` | namespace |
| `describe_index_stats` | `pinecone describe_index_stats` | - |

### ChromaDB

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `add` | `chroma add` | upsert_count, collection_name |
| `query` | `chroma query` | n_results, include, where |
| `get` | `chroma get` | ids, where |
| `update` | `chroma update` | update_count |
| `upsert` | `chroma upsert` | upsert_count |
| `delete` | `chroma delete` | ids, where |
| `count` | `chroma count` | - |
| `peek` | `chroma peek` | limit |

### Qdrant

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `search` | `qdrant search` | top_k, collection, with_payload, score_threshold |
| `upsert` | `qdrant upsert` | upsert_count, collection |
| `delete` | `qdrant delete` | collection |
| `retrieve` | `qdrant retrieve` | ids_count, collection |
| `scroll` | `qdrant scroll` | limit, collection |
| `recommend` | `qdrant recommend` | positive_ids, strategy |
| `count` | `qdrant count` | collection |

### Weaviate (v4 API)

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `near_vector` | `weaviate near_vector` | top_k, certainty, distance |
| `near_text` | `weaviate near_text` | top_k, query |
| `hybrid` | `weaviate hybrid` | top_k, alpha |
| `bm25` | `weaviate bm25` | top_k, query |
| `fetch_objects` | `weaviate fetch_objects` | limit |
| `insert` | `weaviate insert` | upsert_count |
| `insert_many` | `weaviate insert_many` | upsert_count |
| `delete_by_id` | `weaviate delete_by_id` | - |
| `delete_many` | `weaviate delete_many` | delete_count |

### Milvus

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `search` | `milvus search` | top_k, filter, collection |
| `query` | `milvus query` | filter, collection |
| `insert` | `milvus insert` | upsert_count, collection |
| `upsert` | `milvus upsert` | upsert_count, collection |
| `delete` | `milvus delete` | delete_count, collection |
| `get` | `milvus get` | ids_count, collection |
| `create_collection` | `milvus create_collection` | dimension |
| `drop_collection` | `milvus drop_collection` | - |

### LanceDB

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `search` | `lancedb search` | top_k, table |
| `add` | `lancedb add` | upsert_count, table |
| `update` | `lancedb update` | where filter |
| `delete` | `lancedb delete` | where filter |
| `create_table` | `lancedb create_table` | table name |
| `drop_table` | `lancedb drop_table` | table name |
| `open_table` | `lancedb open_table` | table name |

### MongoDB Atlas Vector

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `aggregate` | `mongodb aggregate` | $vectorSearch info, pipeline_stages |
| `insert_one` | `mongodb insert_one` | collection |
| `insert_many` | `mongodb insert_many` | upsert_count, collection |
| `update_one` | `mongodb update_one` | collection |
| `update_many` | `mongodb update_many` | matched_count, modified_count |
| `delete_one` | `mongodb delete_one` | collection |
| `delete_many` | `mongodb delete_many` | deleted_count |
| `find` | `mongodb find` | limit, collection |
| `find_one` | `mongodb find_one` | collection |

### pgvector

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| Vector query (`<->`) | `pgvector query` | distance_type, top_k, table |
| Vector query (`<=>`) | `pgvector query` | distance_type (cosine), top_k |
| Vector query (`<#>`) | `pgvector query` | distance_type (inner_product) |
| Insert | `pgvector insert` | table |
| Batch insert | `pgvector batch` | batch_size |

### Redis Vector

| Operation | Span Name | Captured Attributes |
|-----------|-----------|---------------------|
| `search` | `redis vector_search` | top_k, index, return_fields |
| `load` | `redis load` | upsert_count, index |
| `query` | `redis query` | index |
| `ft_search` | `redis ft_search` | knn, index |
| `ft_create` | `redis ft_create` | index |
| `ft_dropindex` | `redis ft_dropindex` | index |

## Semantic Conventions

All vector DB spans follow OpenTelemetry semantic conventions plus TraceAI extensions:

### Standard Attributes

```
db.system = "pinecone" | "chroma" | "qdrant" | ...
db.operation.name = "query" | "upsert" | "delete" | ...
db.namespace = "collection-name" or "index-name"
```

### Vector-Specific Attributes

```
# Query operations
db.vector.query.top_k = 10
db.vector.query.filter = "{...}"
db.vector.query.include_metadata = true

# Results
db.vector.results.count = 10

# Upsert operations
db.vector.upsert.count = 100

# Collection info
db.vector.collection.name = "my-collection"
db.vector.index.name = "my-index"
```

## Examples

See the `examples/` directory in each package for complete working examples:

| Package | Example File | Description |
|---------|--------------|-------------|
| Pinecone | `examples/basic_rag.py` | RAG pipeline with OpenAI |
| ChromaDB | `examples/semantic_search.py` | Semantic search with filters |
| Qdrant | `examples/similarity_search.py` | Similarity search with recommendations |
| Weaviate | `examples/semantic_search.py` | Hybrid and BM25 search |
| Milvus | `examples/vector_search.py` | Vector search with MilvusClient |
| LanceDB | `examples/local_rag.py` | Local/embedded RAG |
| MongoDB | `examples/atlas_vector_search.py` | $vectorSearch aggregation |
| pgvector | `examples/postgres_vector_search.py` | L2, cosine, inner product |
| Redis | `examples/redis_vector_search.py` | RediSearch with KNN |

## Testing

### Unit Tests

Each package includes unit tests that don't require database connections:

```bash
cd python/frameworks/pinecone
pytest tests/ -v
```

### E2E Integration Tests

For end-to-end testing with real databases:

```bash
# Start databases with Docker
cd python/frameworks/tests_e2e
docker-compose up -d

# Run tests
pytest tests_e2e/ -v -m integration
```

See `tests_e2e/README.md` for detailed instructions.

## Combining with LLM Instrumentation

Vector DB instrumentation works seamlessly with LLM instrumentation for full RAG observability:

```python
from fi_instrumentation import register
from traceai_openai import OpenAIInstrumentor
from traceai_pinecone import PineconeInstrumentor

# Register tracing
trace_provider = register(project_name="full-rag-app")

# Instrument both OpenAI and Pinecone
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
PineconeInstrumentor().instrument(tracer_provider=trace_provider)

# Now both LLM calls and vector DB operations are traced
# in a single unified trace
```

This creates traces like:

```
[RAG Query]
├── [pinecone query] - Vector search
│   └── top_k=5, results_count=5, latency=45ms
├── [openai chat.completions.create] - Generate answer
│   └── model=gpt-4, tokens=350, latency=1200ms
└── Total latency: 1245ms
```

## Troubleshooting

### Common Issues

1. **No spans created**: Ensure you call `instrument()` BEFORE creating the client
2. **Import errors**: Install both the instrumentor and the database client
3. **Missing attributes**: Some attributes only appear for certain operations

### Debug Logging

Enable debug logging to see instrumentation details:

```python
import logging
logging.getLogger("traceai_pinecone").setLevel(logging.DEBUG)
```

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines on adding new vector database integrations.
