# Weaviate tracing for Java

Trace Weaviate collection queries and object writes with retrieval counts, operation names, latency and failure status in Future AGI.

## Choose your client

- **Weaviate client6:** use `TracedWeaviateCollection` or `TracedWeaviateCollectionAsync`. Requires Java 17+, `io.weaviate:client6` 6.0.0–6.3.2 and Weaviate server 1.32+.
- **Legacy client v5:** keep using `TracedWeaviateClient` with `io.weaviate:client:5.5.0`. This entry point is retained for existing applications; it does not accept client6. Weaviate has discontinued v5, so use client6 for new applications.

The v6 adapters are included in this source tree, not in the published traceAI `1.0.0` artifact. Until the next Java release, [build the Java SDK locally](../README.md#build-and-test). Do not install `1.0.0` from Central expecting the new classes.

## Install

After building and installing this checkout locally, add these dependencies. For a released build, use the Java SDK version that includes the v6 adapters instead of the locally installed version below.

```xml
<dependency>
    <groupId>com.futureagi</groupId>
    <artifactId>traceai-java-weaviate</artifactId>
    <version>1.0.0</version>
</dependency>
<dependency>
    <groupId>io.weaviate</groupId>
    <artifactId>client6</artifactId>
    <version>6.3.2</version>
</dependency>
```

Both Weaviate client dependencies are provided-scope in traceAI. Add only the SDK your application uses; the v6 adapter does not require the v5 client at runtime.

## Configure tracing

Set your Future AGI connection settings in the application environment. Keep credentials outside source control.

```bash
export FI_BASE_URL="https://api.futureagi.com"
export FI_API_KEY="<your-api-key>"
export FI_SECRET_KEY="<your-secret-key>"
export FI_PROJECT_NAME="weaviate-java"
export FI_HIDE_ERROR_DETAILS="true"
```

traceAI sends OTLP/HTTP to `${FI_BASE_URL}/tracer/v1/traces`. If your application already owns an OpenTelemetry provider, pass `new FITracer(existingTracer, TraceConfig.builder().hideErrorDetails(true).build())` to the adapter instead of calling `TraceAI.initFromEnvironment()`.

## Query a collection

This example connects to a local Weaviate server. It expects an `Article` collection with a searchable `title` property. Use your existing configured client for a hosted server; the adapter does not manage authentication or connections.

```java
import ai.traceai.TraceAI;
import ai.traceai.weaviate.TracedWeaviateCollection;
import io.weaviate.client6.v1.api.WeaviateClient;
import io.weaviate.client6.v1.api.collections.query.Bm25;

public class WeaviateExample {
    public static void main(String[] args) throws Exception {
        TraceAI.initFromEnvironment();
        try (var client = WeaviateClient.connectToLocal(c -> c
                .host("127.0.0.1")
                .port(Integer.parseInt(System.getenv().getOrDefault("WEAVIATE_HTTP_PORT", "8080")))
                .grpcPort(Integer.parseInt(System.getenv().getOrDefault("WEAVIATE_GRPC_PORT", "50051"))))) {
            var articles = TracedWeaviateCollection.of(client, "Article", TraceAI.getTracer());
            var results = articles.bm25(Bm25.of("observability", q -> q.limit(5).returnProperties("title")));
            System.out.println("Matches: " + results.objects().size());
        } finally {
            TraceAI.shutdown();
        }
    }
}
```

Open your project in Future AGI and find the `Weaviate bm25` retrieval span. It records the collection, requested result limit and returned object count. When called inside an existing span, it is a child of that span.

For tenant-scoped or typed collections, wrap the configured handle directly:

```java
var handle = client.collections.use("Article").withTenant("tenant-a");
var articles = new TracedWeaviateCollection<>("Article", handle, tracer);
```

For asynchronous calls, wrap a handle from `WeaviateClientAsync` with `TracedWeaviateCollectionAsync`. Spans end when the SDK future completes, fails or is cancelled. The adapter returns that original future and preserves the SDK's cancellation behavior.

## Coverage and data handling

Both v6 adapters support `fetchObjects`, `fetchObjectById`, `nearVector`, `nearText`, `nearObject`, `bm25`, `hybrid`, `insert`, `insertMany`, `update`, `replace`, `deleteById` and `deleteMany`. Queries accept native SDK request objects; grouped overloads also accept `GroupBy`. Filters, named vectors, return options and handle defaults pass through unchanged.

Only calls through the adapter create semantic spans. Administrative operations, aggregation, generation, multimodal queries, pagination helpers and streaming batch APIs are not wrapped. Call those on your original client or `unwrap()`; they do not gain semantic tracing from this package. The adapter never closes your client or registers a second OpenTelemetry provider.

The v6 adapters record metadata and counts, not query text, vectors, document properties, object IDs or batch error bodies. Partial batch failures mark the span as an error without changing the returned response. `FI_HIDE_ERROR_DETAILS=true` also removes exception messages and stack traces from these spans. HTTP/gRPC instrumentation and the legacy v5 wrapper have separate capture behavior.

## Migrate from v5

Keep the legacy wrapper while your application uses `io.weaviate:client`. Migrate the application to [Weaviate's collection-based Java API](https://docs.weaviate.io/weaviate/client-libraries/java) and confirm server compatibility before replacing the tracing entry point. The migration is not a constructor rename: v6 uses native request objects and typed responses instead of the v5 GraphQL builders and `Result<T>` values.

If the new adapter class is missing, check the traceAI artifact version. If connection initialization fails, check the Weaviate HTTP and gRPC ports and server version before configuring tracing.
