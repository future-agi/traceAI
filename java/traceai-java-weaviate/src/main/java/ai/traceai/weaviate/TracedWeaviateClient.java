package ai.traceai.weaviate;

import ai.traceai.*;
import io.weaviate.client.WeaviateClient;
import io.weaviate.client.base.Result;
import io.weaviate.client.v1.graphql.model.GraphQLResponse;
import io.weaviate.client.v1.graphql.query.argument.NearVectorArgument;
import io.weaviate.client.v1.graphql.query.fields.Field;
import io.weaviate.client.v1.data.model.WeaviateObject;
import io.weaviate.client.v1.batch.model.ObjectGetResponse;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Weaviate Java client.
 * Wraps the WeaviateClient to provide automatic tracing of all vector operations.
 *
 * <p>Usage:</p>
 * <pre>
 * WeaviateClient client = WeaviateClient.builder()
 *     .scheme("http")
 *     .host("localhost:8080")
 *     .build();
 * TracedWeaviateClient traced = new TracedWeaviateClient(client);
 *
 * Result&lt;GraphQLResponse&gt; result = traced.nearVectorSearch(
 *     "Article", queryVector, 10, "title", "content");
 * </pre>
 */
public class TracedWeaviateClient {

    private final WeaviateClient client;
    private final FITracer tracer;

    /**
     * Creates a new traced Weaviate client with the given client and tracer.
     *
     * @param client the WeaviateClient to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedWeaviateClient(WeaviateClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Weaviate client using the global TraceAI tracer.
     *
     * @param client the WeaviateClient to wrap
     */
    public TracedWeaviateClient(WeaviateClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Performs a near vector search with tracing.
     *
     * @param className   the class/collection name
     * @param vector      the query vector
     * @param limit       maximum number of results
     * @param fieldNames  the fields to return
     * @return the GraphQL response
     */
    public Result<GraphQLResponse> nearVectorSearch(String className, Float[] vector, int limit, String... fieldNames) {
        Span span = tracer.startSpan("Weaviate NearVector Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "weaviate");
            span.setAttribute("weaviate.class", className);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) limit);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vector.length);

            // Build fields
            Field[] fields = new Field[fieldNames.length + 1];
            for (int i = 0; i < fieldNames.length; i++) {
                fields[i] = Field.builder().name(fieldNames[i]).build();
            }
            // Add _additional field for distance/certainty
            fields[fieldNames.length] = Field.builder()
                .name("_additional")
                .fields(Field.builder().name("distance").build(),
                        Field.builder().name("certainty").build(),
                        Field.builder().name("id").build())
                .build();

            // Execute search
            Result<GraphQLResponse> result = client.graphQL().get()
                .withClassName(className)
                .withFields(fields)
                .withNearVector(NearVectorArgument.builder().vector(vector).build())
                .withLimit(limit)
                .run();

            // Capture results
            if (result.hasErrors()) {
                span.setAttribute("weaviate.has_errors", true);
                if (result.getError() != null) {
                    span.setAttribute("weaviate.error", result.getError().toString());
                }
            } else if (result.getResult() != null && result.getResult().getData() != null) {
                Map<String, Object> data = (Map<String, Object>) result.getResult().getData();
                if (data.containsKey("Get")) {
                    Map<String, Object> get = (Map<String, Object>) data.get("Get");
                    if (get.containsKey(className)) {
                        List<?> items = (List<?>) get.get(className);
                        span.setAttribute("weaviate.results_count", (long) items.size());
                    }
                }
            }

            span.setStatus(result.hasErrors() ? StatusCode.ERROR : StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Weaviate search failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Creates an object with tracing.
     *
     * @param className  the class/collection name
     * @param properties the object properties
     * @param vector     the vector (optional)
     * @return the created object
     */
    public Result<WeaviateObject> createObject(String className, Map<String, Object> properties, Float[] vector) {
        Span span = tracer.startSpan("Weaviate Create Object", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "weaviate");
            span.setAttribute("weaviate.class", className);

            if (vector != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vector.length);
            }

            // Execute create
            var creator = client.data().creator()
                .withClassName(className)
                .withProperties(properties);

            if (vector != null) {
                creator.withVector(vector);
            }

            Result<WeaviateObject> result = creator.run();

            // Capture result
            if (result.hasErrors()) {
                span.setAttribute("weaviate.has_errors", true);
            } else if (result.getResult() != null) {
                span.setAttribute("weaviate.object_id", result.getResult().getId());
            }

            span.setStatus(result.hasErrors() ? StatusCode.ERROR : StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Weaviate create object failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Batch imports objects with tracing.
     *
     * @param objects the objects to import
     * @return the batch result
     */
    public Result<ObjectGetResponse[]> batchImport(WeaviateObject... objects) {
        Span span = tracer.startSpan("Weaviate Batch Import", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "weaviate");
            span.setAttribute("weaviate.batch_size", (long) objects.length);

            if (objects.length > 0 && objects[0].getClassName() != null) {
                span.setAttribute("weaviate.class", objects[0].getClassName());
            }

            // Execute batch
            Result<ObjectGetResponse[]> result = client.batch().objectsBatcher()
                .withObjects(objects)
                .run();

            // Capture result
            if (result.hasErrors()) {
                span.setAttribute("weaviate.has_errors", true);
            } else if (result.getResult() != null) {
                span.setAttribute("weaviate.imported_count", (long) result.getResult().length);
            }

            span.setStatus(result.hasErrors() ? StatusCode.ERROR : StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Weaviate batch import failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Deletes an object with tracing.
     *
     * @param className the class/collection name
     * @param id        the object ID
     * @return the delete result
     */
    public Result<Boolean> deleteObject(String className, String id) {
        Span span = tracer.startSpan("Weaviate Delete Object", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "weaviate");
            span.setAttribute("weaviate.class", className);
            span.setAttribute("weaviate.object_id", id);

            // Execute delete
            Result<Boolean> result = client.data().deleter()
                .withClassName(className)
                .withID(id)
                .run();

            span.setStatus(result.hasErrors() ? StatusCode.ERROR : StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Weaviate delete object failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets an object by ID with tracing.
     *
     * @param className the class/collection name
     * @param id        the object ID
     * @return the object
     */
    public Result<List<WeaviateObject>> getObject(String className, String id) {
        Span span = tracer.startSpan("Weaviate Get Object", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            // Set attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "weaviate");
            span.setAttribute("weaviate.class", className);
            span.setAttribute("weaviate.object_id", id);

            // Execute get
            Result<List<WeaviateObject>> result = client.data().objectsGetter()
                .withClassName(className)
                .withID(id)
                .run();

            // Capture result
            if (!result.hasErrors() && result.getResult() != null) {
                span.setAttribute("weaviate.found", !result.getResult().isEmpty());
            }

            span.setStatus(result.hasErrors() ? StatusCode.ERROR : StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Weaviate get object failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying WeaviateClient.
     *
     * @return the wrapped WeaviateClient
     */
    public WeaviateClient unwrap() {
        return client;
    }
}
