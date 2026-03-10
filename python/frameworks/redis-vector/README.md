# traceAI-redis

OpenTelemetry instrumentation for [Redis Vector Search](https://redis.io/docs/stack/search/reference/vectors/) (RediSearch/RedisVL).

## Installation

```bash
pip install traceAI-redis
```

## Quick Start

### Using RedisVL (Recommended)

```python
from fi_instrumentation import register
from traceai_redis import RedisInstrumentor
from redisvl.index import SearchIndex
from redisvl.query import VectorQuery

trace_provider = register(project_name="my-rag-app")
RedisInstrumentor().instrument(tracer_provider=trace_provider)

index = SearchIndex.from_yaml("schema.yaml")

# All operations are traced
query = VectorQuery(
    vector=[0.1, 0.2, ...],
    vector_field_name="embedding",
    return_fields=["content"],
    num_results=5
)
results = index.search(query)
```

### Using redis-py directly

```python
from redis import Redis
from redis.commands.search.query import Query

client = Redis()

# FT.SEARCH with vector queries are traced
query = Query("*=>[KNN 5 @embedding $vec AS score]").dialect(2)
results = client.ft("documents").search(query, query_params={"vec": vector_bytes})
```

## Instrumented Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `search` | `redis vector_search` | Vector similarity search |
| `load` | `redis load` | Load vectors into index |
| `query` | `redis query` | Execute search query |
| `ft_search` | `redis ft_search` | RediSearch FT.SEARCH |
| `ft_create` | `redis ft_create` | Create search index |
| `ft_dropindex` | `redis ft_dropindex` | Drop search index |
| `hset` | `redis hset` | Set hash fields (used for vectors) |
| `pipeline` | `redis pipeline` | Batch operations |

## License

Apache License 2.0
