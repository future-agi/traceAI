package ai.traceai.chromadb;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import tech.amikos.chromadb.Client;
import tech.amikos.chromadb.Collection;
import tech.amikos.chromadb.Embedding;
import tech.amikos.chromadb.embeddings.EmbeddingFunction;
import tech.amikos.chromadb.handler.ApiException;
import tech.amikos.chromadb.model.QueryEmbedding.IncludeEnum;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowableOfType;

/** Real released client, synthetic API-v1 server; no model, database or vendor credentials. */
class TracedChromaCollectionCompatibilityTest {
    private static final String COLLECTION = "legacy-documents";
    private static final String COLLECTION_PATH = "/api/v1/collections/collection-id";
    private static final List<Embedding> EMBEDDINGS = List.of(new Embedding(List.of(0.1f, 0.2f)));
    private static final List<String> IDS = List.of("doc-1");
    private static final List<String> DOCUMENTS = List.of("Synthetic document");
    private static final Map<String, Object> WHERE = Map.of("category", "example");
    private static final Map<String, Object> WHERE_DOCUMENT = Map.of("$contains", "Synthetic");

    private HttpServer server;
    private final List<Request> requests = new CopyOnWriteArrayList<>();
    private volatile boolean rejectV1;
    private volatile boolean failUpdate;
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FITracer tracer;
    private Client client;

    @BeforeEach
    void setUp() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", this::respond);
        server.start();
        client = new Client("http://127.0.0.1:" + server.getAddress().getPort());
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("chromadb-test"), TraceConfig.builder()
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
    }

    @AfterEach
    void tearDown() {
        if (provider != null) {
            provider.close();
        }
        if (server != null) {
            server.stop(0);
        }
    }

    @Test
    void releasedClientPreservesLegacyOperationsAndOneChildSpanPerCall() throws Exception {
        Collection collection = client.getCollection(COLLECTION, new FixedEmbeddings());
        TracedChromaCollection traced = new TracedChromaCollection(collection, tracer, COLLECTION);
        Span parent = provider.get("app").spanBuilder("application").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            traced.add(EMBEDDINGS, List.of(Map.of("category", "example")), DOCUMENTS, IDS);
            traced.upsert(EMBEDDINGS, null, DOCUMENTS, IDS);
            assertThat(traced.query(List.of("Synthetic query"), 2, WHERE, WHERE_DOCUMENT,
                    List.of(IncludeEnum.DOCUMENTS, IncludeEnum.DISTANCES)).getIds())
                    .containsExactly(IDS);
            assertThat(traced.get(IDS, Map.of("category", "example"), WHERE_DOCUMENT).getIds())
                    .containsExactlyElementsOf(IDS);
            assertThat(traced.count()).isEqualTo(1);
            traced.delete(IDS, WHERE, WHERE_DOCUMENT);
            assertThat(Span.current()).isSameAs(parent);
        } finally {
            parent.end();
        }
        assertThat(traced.unwrap()).isSameAs(collection);
        assertThat(requests).extracting(Request::route).containsExactly(
                "GET /api/v1/collections/" + COLLECTION,
                "POST " + COLLECTION_PATH + "/add",
                "POST " + COLLECTION_PATH + "/upsert",
                "POST " + COLLECTION_PATH + "/query",
                "POST " + COLLECTION_PATH + "/get",
                "GET " + COLLECTION_PATH + "/count",
                "POST " + COLLECTION_PATH + "/delete");
        assertThat(requests.get(1).json().getAsJsonArray("ids").get(0).getAsString()).isEqualTo("doc-1");
        JsonObject query = requests.get(3).json();
        assertThat(query.get("n_results").getAsInt()).isEqualTo(2);
        assertThat(query.getAsJsonArray("query_embeddings").get(0).getAsJsonArray().size()).isEqualTo(2);
        assertThat(query.getAsJsonObject("where").get("category").getAsString()).isEqualTo("example");
        assertThat(query.getAsJsonObject("where_document").get("$contains").getAsString()).isEqualTo("Synthetic");

        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).extracting(SpanData::getName).containsExactly(
                "ChromaDB Add", "ChromaDB Upsert", "ChromaDB Query", "ChromaDB Get",
                "ChromaDB Count", "ChromaDB Delete", "application");
        for (SpanData span : spans.subList(0, 6)) {
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getTraceId()).isEqualTo(parent.getSpanContext().getTraceId());
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.DB_SYSTEM))).isEqualTo("chromadb");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.DB_VECTOR_COLLECTION_NAME))).isEqualTo(COLLECTION);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE))).isNull();
        }
        assertThat(spans.get(2).getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND))).isEqualTo("RETRIEVER");
        assertThat(spans.get(2).getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT))).isEqualTo(1L);
        assertThat(spans.get(2).getAttributes().get(AttributeKey.doubleKey("chromadb.top_distance"))).isEqualTo(0.25);
    }

    @Test
    void releasedClientCannotFetchCollectionWhenServerRejectsV1() {
        // Chroma 1.5.9 server.rs routes GET /api/v1/{*any} to HTTP 410.
        rejectV1 = true;
        ApiException failure = catchThrowableOfType(
                () -> client.getCollection(COLLECTION, new FixedEmbeddings()), ApiException.class);
        assertThat(failure.getCode()).isEqualTo(410);
        assertThat(failure.getResponseBody()).contains("The v1 API is deprecated");
        assertThat(requests).extracting(Request::route)
                .containsExactly("GET /api/v1/collections/" + COLLECTION);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
    }

    @Test
    void wrappedOperationPreserves410AndEndsAnErrorSpanWithoutRetryOrFallback() throws Exception {
        TracedChromaCollection traced = new TracedChromaCollection(
                client.getCollection(COLLECTION, new FixedEmbeddings()), tracer, COLLECTION);
        rejectV1 = true;
        ApiException failure = catchThrowableOfType(traced::count, ApiException.class);
        assertThat(failure.getCode()).isEqualTo(410);
        assertThat(requests).extracting(Request::route).containsExactly(
                "GET /api/v1/collections/" + COLLECTION, "GET " + COLLECTION_PATH + "/count");
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getName()).isEqualTo("ChromaDB Count");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEmpty();
            assertThat(span.getEvents()).hasSize(1);
        });
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void updateEmbeddingsDelegatesAndExportsOnlyOperationMetadata() throws Exception {
        TracedChromaCollection traced = new TracedChromaCollection(
                client.getCollection(COLLECTION, new FixedEmbeddings()), tracer, COLLECTION);
        Object result = traced.updateEmbeddings(EMBEDDINGS, List.of(Map.of("category", "private-metadata")),
                List.of("private-document"), List.of("private-id"));
        assertThat(result).isEqualTo(true);
        assertThat(requests).extracting(Request::route).containsExactly(
                "GET /api/v1/collections/" + COLLECTION, "POST " + COLLECTION_PATH + "/update");
        JsonObject body = requests.get(1).json();
        assertThat(body.getAsJsonArray("ids").get(0).getAsString()).isEqualTo("private-id");
        assertThat(body.getAsJsonArray("documents").get(0).getAsString()).isEqualTo("private-document");
        assertThat(body.getAsJsonArray("metadatas").get(0).getAsJsonObject()
                .get("category").getAsString()).isEqualTo("private-metadata");
        assertThat(body.getAsJsonArray("embeddings").get(0).getAsJsonArray().get(0).getAsFloat()).isEqualTo(0.1f);

        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getName()).isEqualTo("ChromaDB Update");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        });
        var wireSpan = exportOverOtlp();
        assertThat(wireSpan.getName()).isEqualTo("ChromaDB Update");
        assertThat(wireSpan.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_OK);
        Map<String, AnyValue> attributes = wireSpan.getAttributesList().stream()
                .collect(Collectors.toMap(KeyValue::getKey, KeyValue::getValue));
        assertThat(attributes.keySet()).containsExactlyInAnyOrder(
                SemanticConventions.DB_SYSTEM, SemanticConventions.DB_VECTOR_COLLECTION_NAME,
                SemanticConventions.FI_SPAN_KIND, SemanticConventions.GEN_AI_SPAN_KIND, "chromadb.update_count");
        assertThat(attributes.get(SemanticConventions.DB_SYSTEM).getStringValue()).isEqualTo("chromadb");
        assertThat(attributes.get(SemanticConventions.DB_VECTOR_COLLECTION_NAME).getStringValue()).isEqualTo(COLLECTION);
        assertThat(attributes.get(SemanticConventions.FI_SPAN_KIND).getStringValue()).isEqualTo("VECTOR_DB");
        assertThat(attributes.get("chromadb.update_count").getIntValue()).isEqualTo(1);
        assertThat(wireSpan.toString()).doesNotContain("private-document", "private-id", "private-metadata");
    }

    @Test
    void updateEmbeddingsKeepsClientEmbeddingFunctionAndOptionalArguments() throws Exception {
        TracedChromaCollection traced = new TracedChromaCollection(
                client.getCollection(COLLECTION, new FixedEmbeddings()), tracer, COLLECTION);
        assertThat(traced.updateEmbeddings(null, null, DOCUMENTS, IDS)).isEqualTo(true);
        assertThat(requests.get(1).json().getAsJsonArray("embeddings").get(0)
                .getAsJsonArray().get(1).getAsFloat()).isEqualTo(0.2f);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void failedUpdatePreservesClientExceptionAndHidesErrorDetailsOnOtlp() throws Exception {
        TracedChromaCollection traced = new TracedChromaCollection(
                client.getCollection(COLLECTION, new FixedEmbeddings()), tracer, COLLECTION);
        failUpdate = true;
        var failure = catchThrowableOfType(() -> traced.updateEmbeddings(EMBEDDINGS, null, null, IDS),
                tech.amikos.chromadb.ChromaException.class);
        assertThat(failure.getCause()).isInstanceOf(ApiException.class);
        assertThat(((ApiException) failure.getCause()).getCode()).isEqualTo(500);
        assertThat(requests).hasSize(2);
        var wireSpan = exportOverOtlp();
        assertThat(wireSpan.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
        assertThat(wireSpan.getStatus().getMessage()).isEmpty();
        assertThat(wireSpan.getEventsList()).singleElement().satisfies(event -> {
            assertThat(event.getName()).isEqualTo("exception");
            assertThat(event.getAttributesList()).extracting(KeyValue::getKey).containsExactly("exception.type");
        });
        assertThat(wireSpan.toString()).doesNotContain("private-server-error", "exception.stacktrace");
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    private io.opentelemetry.proto.trace.v1.Span exportOverOtlp() throws IOException {
        List<byte[]> payloads = new CopyOnWriteArrayList<>();
        server.createContext("/v1/traces", exchange -> {
            payloads.add(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        try (OtlpHttpSpanExporter otlp = OtlpHttpSpanExporter.builder()
                .setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces").build()) {
            assertThat(otlp.export(exporter.getFinishedSpanItems()).join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        }
        assertThat(payloads).hasSize(1);
        var exported = ExportTraceServiceRequest.parseFrom(payloads.get(0));
        assertThat(exported.getResourceSpansList()).hasSize(1);
        assertThat(exported.getResourceSpans(0).getScopeSpansList()).hasSize(1);
        assertThat(exported.getResourceSpans(0).getScopeSpans(0).getSpansList()).hasSize(1);
        return exported.getResourceSpans(0).getScopeSpans(0).getSpans(0);
    }

    private void respond(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        requests.add(new Request(exchange.getRequestMethod() + " " + path,
                new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)));
        int status = 200;
        String body;
        if (failUpdate && path.equals(COLLECTION_PATH + "/update")) {
            status = 500;
            body = "{\"error\":\"private-server-error\"}";
        } else if (rejectV1 && exchange.getRequestMethod().equals("GET") && path.startsWith("/api/v1/")) {
            status = 410;
            body = "{\"error\":\"Unimplemented\",\"message\":\"The v1 API is deprecated. Please use /v2 apis\"}";
        } else if (path.equals("/api/v1/collections/" + COLLECTION)) {
            body = "{\"id\":\"collection-id\",\"name\":\"" + COLLECTION + "\",\"metadata\":{}}";
        } else if (path.equals(COLLECTION_PATH + "/count")) {
            body = "1";
        } else if (path.equals(COLLECTION_PATH + "/query")) {
            body = "{\"ids\":[[\"doc-1\"]],\"documents\":[[\"Synthetic document\"]],\"distances\":[[0.25]]}";
        } else if (path.equals(COLLECTION_PATH + "/get")) {
            body = "{\"ids\":[\"doc-1\"],\"documents\":[\"Synthetic document\"]}";
        } else if (List.of("/add", "/upsert", "/delete", "/update").stream()
                .anyMatch(operation -> path.equals(COLLECTION_PATH + operation))) {
            body = "true";
        } else {
            status = 404;
            body = "{\"error\":\"Unexpected test route\"}";
        }
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) {
            output.write(bytes);
        }
    }

    private record Request(String route, String body) {
        JsonObject json() {
            return JsonParser.parseString(body).getAsJsonObject();
        }
    }

    private static final class FixedEmbeddings implements EmbeddingFunction {
        @Override
        public Embedding embedQuery(String query) {
            return EMBEDDINGS.get(0);
        }

        @Override
        public List<Embedding> embedDocuments(List<String> documents) {
            List<Embedding> result = new ArrayList<>();
            for (String ignored : documents) {
                result.add(EMBEDDINGS.get(0));
            }
            return result;
        }

        @Override
        public List<Embedding> embedDocuments(String[] documents) {
            return embedDocuments(List.of(documents));
        }
    }
}
