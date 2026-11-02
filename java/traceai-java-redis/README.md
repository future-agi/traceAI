# Redis vector search

Trace Redis Search queries and vector document operations with `TracedRedisVectorSearch`.

## Compatibility

Use Java 17 or later, Redis with the Search module, and **Jedis 5.1.x or 5.2.x**. This module uses `JedisPooled` and does not support Jedis 6, 7 or 8. In particular, Jedis 8 removes `JedisPooled`; do not upgrade an application using this wrapper to Jedis 8.

Jedis is a provided dependency. Add `redis.clients:jedis:5.2.0` to your application alongside `com.futureagi:traceai-java-redis`, using the same released TraceAI version as your core SDK. Initialize TraceAI with the [Java SDK setup](../README.md) before creating the wrapper.

This is an application-level vector wrapper, not automatic instrumentation of all Jedis commands. The OpenTelemetry Java agent can trace generic Jedis database operations instead when vector-specific attributes are not needed. If you use the agent with this wrapper, expect a vector-operation span and lower-level database child spans; do not wrap the same search twice.

## Create, write and search

The convenience index uses Redis HASH documents with the `doc:` prefix and FLOAT32 vectors. `distanceMetric` is `COSINE`, `L2` or `IP`; `algorithm` is `FLAT` or `HNSW`. These arguments are case insensitive.

```java
import ai.traceai.redis.TracedRedisVectorSearch;
import redis.clients.jedis.JedisPooled;
import java.util.Map;

try (JedisPooled jedis = new JedisPooled("localhost", 6379)) {
    TracedRedisVectorSearch traced = new TracedRedisVectorSearch(jedis);
    traced.createIndex("products", "product_vector", 3, "COSINE", "HNSW");
    traced.addDocument("doc:1", "product_vector", new float[]{0.1f, 0.2f, 0.3f},
            Map.of("title", "Travel mug"));
    var results = traced.vectorSearch("products", "product_vector",
            new float[]{0.1f, 0.2f, 0.3f}, 25);
    System.out.println(results.getDocuments().size());
}
```

Create each index once. The example returns at most 25 documents, ordered by distance, with the `score` field. The number returned can be smaller when fewer documents match. Field names are preserved in the schema and writes, and escaped with Jedis's query utility when constructing convenience queries. Use the native-query route below for other schemas.

The original constructors, `unwrap()`, and convenience methods remain available:

- `vectorSearch(index, vector, topK[, filter])` uses the field from a successful `createIndex` call on the same wrapper. For an externally created index, it defaults to `embedding`; supply the explicit-field overload for another field.
- `addDocument(key, vector, metadata)` uses the sole vector field shared by indexes created through the wrapper. With no known index or with indexes using different fields, it writes `embedding` to preserve legacy behavior; supply the explicit-field overload to write to a particular custom index. Field discovery is local to the wrapper, not a Redis schema lookup.
- Metadata must not use the vector field's name. This prevents metadata from replacing the binary vector.
- An optional filter is a native Redis Search expression. Fields in that filter must already be indexed; the convenience `createIndex` method only creates the vector field. Use Jedis to define a richer schema.

## Trace a native Jedis query

Keep query semantics in your existing Jedis code when you need a richer schema, additional return fields, different paging or a different dialect:

```java
import redis.clients.jedis.search.Query;

Query query = new Query("*").limit(0, 25).returnFields("title").dialect(2);
var results = traced.search("products", query);
```

`search` passes the same query to Jedis without rewriting it. It records a generic `Redis Search` span (`UNKNOWN`) with the index and returned-document count, not vector or retriever attributes, because Jedis queries can also be nonvector. Use the returned `SearchResult` normally. The application owns the Jedis client's lifecycle.

## Inspect traces

Open your project in Future AGI. Convenience vector searches appear as `Redis Vector Search` (`RETRIEVER`); arbitrary Jedis queries appear as `Redis Search` (`UNKNOWN`). Index creation, document writes and deletion use `VECTOR_DB` spans. Convenience searches include the vector field, dimensions and requested top-K.

Vector values, document metadata and native query text/parameters are not recorded. Convenience filter expressions and document keys are omitted when the tracer's `hideInputs` setting is enabled. Configure `hideErrorDetails` to omit exception messages and stack traces as well.
