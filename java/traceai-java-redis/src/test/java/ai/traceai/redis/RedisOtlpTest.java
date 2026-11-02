package ai.traceai.redis;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import redis.clients.jedis.search.Query;
import redis.clients.jedis.search.SearchResult;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.*;

class RedisOtlpTest {
    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void exportsSingleParentedSpansAndHonorsPrivacyOnTheOtlpWire(boolean hideInputs) throws Exception {
        List<ExportTraceServiceRequest> requests = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            requests.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        try (var jedis = new TracedRedisVectorSearchTest.RecordingJedis();
             var provider = SdkTracerProvider.builder()
                     .addSpanProcessor(SimpleSpanProcessor.create(OtlpHttpSpanExporter.builder()
                             .setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces")
                             .setTimeout(Duration.ofSeconds(5)).build())).build()) {
            jedis.result = new SearchResult.SearchResultBuilder(true, false, true)
                    .build(List.of(50L, "doc:1".getBytes(StandardCharsets.UTF_8),
                            List.of("score".getBytes(StandardCharsets.UTF_8), "0.25".getBytes(StandardCharsets.UTF_8))));
            var otelTracer = provider.get("redis-wire-test");
            var tracer = new FITracer(otelTracer, TraceConfig.builder()
                    .hideInputs(hideInputs).hideOutputs(true).hideErrorDetails(true).build());
            var traced = new TracedRedisVectorSearch(jedis, tracer);
            Span parent = otelTracer.spanBuilder("application").startSpan();
            String successId;
            String genericId;
            String errorId;
            try (Scope scope = parent.makeCurrent()) {
                traced.createIndex("products", "product_vector", 2, "COSINE", "HNSW");
                traced.addDocument("doc:sensitive-key", new float[]{1, 2}, Map.of("title", "sensitive-metadata"));
                assertThat(traced.vectorSearch("products", new float[]{1, 2}, 25, "@title:{sensitive-filter}"))
                        .isSameAs(jedis.result);
                successId = jedis.currentSpanId;
                traced.search("products", new Query("@title:sensitive-query").addParam("secret", "sensitive-param"));
                genericId = jedis.currentSpanId;
                jedis.failure = new IllegalStateException("sensitive-error");
                assertThatThrownBy(() -> traced.vectorSearch("products", new float[]{1, 2}, 25))
                        .hasCause(jedis.failure);
                errorId = jedis.currentSpanId;
                traced.deleteDocument("doc:sensitive-key");
            } finally {
                parent.end();
            }
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
            var flush = provider.forceFlush().join(10, TimeUnit.SECONDS);
            assertThat(flush.isSuccess()).isTrue();
            List<io.opentelemetry.proto.trace.v1.Span> spans = new ArrayList<>();
            for (var request : requests) {
                request.getResourceSpansList().forEach(resource -> resource.getScopeSpansList()
                        .forEach(scope -> spans.addAll(scope.getSpansList())));
            }
            // Six wrapper calls and the application parent, with no nested wrapper spans.
            assertThat(spans).hasSize(7);
            var children = spans.stream().filter(span -> !span.getName().equals("application")).toList();
            assertThat(children).hasSize(6).allSatisfy(span -> {
                assertThat(HexFormat.of().formatHex(span.getParentSpanId().toByteArray()))
                        .isEqualTo(parent.getSpanContext().getSpanId());
                assertThat(HexFormat.of().formatHex(span.getTraceId().toByteArray()))
                        .isEqualTo(parent.getSpanContext().getTraceId());
                assertThat(span.getEndTimeUnixNano()).isGreaterThanOrEqualTo(span.getStartTimeUnixNano());
                assertThat(attributes(span).get(SemanticConventions.DB_SYSTEM).getStringValue()).isEqualTo("redis");
            });
            var success = children.stream().filter(span -> HexFormat.of().formatHex(span.getSpanId().toByteArray())
                    .equals(successId)).findFirst().orElseThrow();
            assertThat(success.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_OK);
            assertThat(attributes(success).get(SemanticConventions.FI_SPAN_KIND).getStringValue()).isEqualTo("RETRIEVER");
            assertThat(attributes(success).get(SemanticConventions.RETRIEVER_TOP_K).getIntValue()).isEqualTo(25);
            assertThat(attributes(success).get(SemanticConventions.EMBEDDING_DIMENSIONS).getIntValue()).isEqualTo(2);
            assertThat(attributes(success).get("redis.vector_field").getStringValue()).isEqualTo("product_vector");
            assertThat(attributes(success).get(SemanticConventions.DB_VECTOR_RESULTS_COUNT).getIntValue()).isEqualTo(1);
            assertThat(attributes(success).get("redis.top_score").getDoubleValue()).isEqualTo(0.25);
            // A native nonvector query is a generic Redis Search operation on the wire.
            var generic = children.stream().filter(span -> HexFormat.of().formatHex(span.getSpanId().toByteArray())
                    .equals(genericId)).findFirst().orElseThrow();
            assertThat(generic.getName()).isEqualTo("Redis Search");
            assertThat(attributes(generic).get(SemanticConventions.FI_SPAN_KIND).getStringValue())
                    .isEqualTo("UNKNOWN");
            assertThat(attributes(generic).get("redis.index_name").getStringValue()).isEqualTo("products");
            assertThat(attributes(generic).get("redis.results.count").getIntValue()).isEqualTo(1);
            assertThat(attributes(generic)).doesNotContainKeys(SemanticConventions.DB_VECTOR_INDEX_NAME,
                    SemanticConventions.DB_VECTOR_RESULTS_COUNT, SemanticConventions.RETRIEVER_TOP_K,
                    SemanticConventions.EMBEDDING_DIMENSIONS, "redis.vector_field");
            var error = children.stream().filter(span -> HexFormat.of().formatHex(span.getSpanId().toByteArray())
                    .equals(errorId)).findFirst().orElseThrow();
            assertThat(error.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
            assertThat(error.getEventsList()).hasSize(1);
            assertThat(attributes(error).get(SemanticConventions.ERROR_TYPE).getStringValue())
                    .isEqualTo(IllegalStateException.class.getName());
            assertThat(requests.toString()).doesNotContain("sensitive-metadata", "sensitive-query",
                    "sensitive-param", "sensitive-error", "exception.message");
            if (hideInputs) {
                assertThat(requests.toString()).doesNotContain("sensitive-", "redis.filter", "redis.key");
            } else {
                assertThat(requests.toString()).contains("sensitive-filter", "sensitive-key");
            }
        } finally {
            server.stop(0);
        }
    }

    private static Map<String, AnyValue> attributes(io.opentelemetry.proto.trace.v1.Span span) {
        return span.getAttributesList().stream().collect(Collectors.toMap(
                attribute -> attribute.getKey(), attribute -> attribute.getValue()));
    }
}
