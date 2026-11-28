# traceAI-redis

Trace RedisVL queries and redis-py search operations in Future AGI, with index names, query metadata, result counts, latency, and errors. Both synchronous and asynchronous clients are supported.

## Install

Use Python 3.10–3.13 and a Redis server with vector search support.

```bash
pip install traceAI-redis
```

Set `FI_API_KEY` and `FI_SECRET_KEY` to your Future AGI credentials. Set `REDIS_URL` if Redis is not running at `redis://localhost:6379`.

## Trace a RedisVL query

This example creates a small index and loads two sample vectors. The vectors are illustrative, not model-generated embeddings.

```python
import os
import struct

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from redisvl.index import SearchIndex
from redisvl.query import VectorQuery
from traceai_redis import RedisInstrumentor

provider = register(project_name="redis-vector-search", project_type=ProjectType.OBSERVE)
RedisInstrumentor().instrument(tracer_provider=provider)

schema = {
    "index": {"name": "traceai-example", "prefix": "traceai-example"},
    "fields": [
        {"name": "content", "type": "text"},
        {
            "name": "embedding",
            "type": "vector",
            "attrs": {
                "dims": 3,
                "algorithm": "flat",
                "datatype": "float32",
                "distance_metric": "cosine",
            },
        },
    ],
}
index = SearchIndex.from_dict(
    schema, redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379")
)
index.create(overwrite=False)
index.load(
    [
        {"content": "Vector search", "embedding": struct.pack("3f", 0.1, 0.2, 0.3)},
        {"content": "Document storage", "embedding": struct.pack("3f", 0.8, 0.1, 0.1)},
    ],
    keys=["traceai-example:1", "traceai-example:2"],
)
query = VectorQuery(
    vector=[0.1, 0.2, 0.3],
    vector_field_name="embedding",
    return_fields=["content"],
    num_results=2,
)
print(index.query(query))
provider.force_flush()
```

Use `index.query(query)` with a RedisVL `VectorQuery`; RedisVL supplies the vector parameters and processes the results. For the lower-level search API, use `index.search(query.query, query_params=query.params)`. With the default RESP2 client, it returns a redis-py `Result` with `.docs` and `.total`; a RESP3 client can return a raw mapping instead. Use `index.query(query)` when you need RedisVL's processed result list.

Open the `redis-vector-search` project in Future AGI to inspect `redis load` and `redis query` spans. A query that internally calls RedisVL `search()` and redis-py `search()` emits one search span, not three.

## Use redis-py directly

After instrumenting as above, you can query the same index through redis-py:

```python
from redis import Redis
from redis.commands.search.query import Query

client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))
query = Query("*=>[KNN 2 @embedding $vec AS score]").return_fields("content", "score").dialect(2)
result = client.ft("traceai-example").search(
    query, query_params={"vec": struct.pack("3f", 0.1, 0.2, 0.3)}
)
print(result.docs)
provider.force_flush()
```

For async applications, use `AsyncSearchIndex` from `redisvl.index` or `Redis` from `redis.asyncio`, and await the corresponding operations. The same `RedisInstrumentor` instruments both client types and keeps spans open until the operation completes.

## Coverage

| API | Span name |
| --- | --- |
| RedisVL `SearchIndex.search` / `AsyncSearchIndex.search` | `redis vector_search` |
| RedisVL `SearchIndex.query` / `AsyncSearchIndex.query` | `redis query` |
| RedisVL `SearchIndex.load` / `AsyncSearchIndex.load` | `redis load` |
| redis-py `ft().search` | `redis ft_search` |
| redis-py `ft().create_index` | `redis ft_create` |
| redis-py `ft().dropindex` | `redis ft_dropindex` |

`redis vector_search` is the legacy span name for the lower-level RedisVL search method; it can also represent a non-vector search. `db.vector.results.count` counts returned documents, including zero. A raw `Result` also supplies `db.vector.results.total` for total matches. Unknown return shapes are preserved without guessing a count.

This package does not instrument `hset`, pipelines, raw `execute_command` calls, or every RedisVL API. For general command tracing, use the maintained [OpenTelemetry Redis instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/redis/redis.html). If you enable it alongside traceAI, the supported `ft()` operations suppress duplicate command spans; other commands retain their own tracing. The command instrumentor has its own data-capture policy, independent of traceAI privacy settings.

Redis's [native OpenTelemetry integration](https://redis.readthedocs.io/en/stable/opentelemetry.html) exports metrics; it is not a replacement for the trace spans used here.

## Privacy and lifecycle

Query text, filters, vector values, loaded document content, and result content are not recorded. Index names, vector field names, and counts are metadata and remain visible.

Error messages can contain query content. Set `FI_HIDE_ERROR_DETAILS=true` before instrumentation to omit exception messages and stack traces while keeping the error type. `FI_HIDE_INPUTS=true` or `FI_HIDE_OUTPUTS=true` also hide error details unless you explicitly override that policy. You can configure it in code instead:

```python
from fi_instrumentation import TraceConfig

# Use this in place of the earlier instrument() call.
RedisInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_error_details=True),
)
```

`RedisInstrumentor().uninstrument()` removes the patches installed by this instrumentor. OpenTelemetry's `suppress_instrumentation()` context manager also disables these spans for an individual operation.

## License

Apache License 2.0
