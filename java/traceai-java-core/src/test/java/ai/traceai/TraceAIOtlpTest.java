package ai.traceai;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.ResourceSpans;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static io.opentelemetry.api.common.AttributeKey.stringKey;
import static org.assertj.core.api.Assertions.assertThat;

class TraceAIOtlpTest {
    private HttpServer server;
    private final List<Request> requests = new CopyOnWriteArrayList<>();

    @BeforeEach
    void startReceiver() throws IOException {
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                requests.add(new Request(exchange.getRequestURI().getPath(),
                    exchange.getRequestHeaders().getFirst("X-Api-Key"),
                    exchange.getRequestHeaders().getFirst("X-Secret-Key"),
                    exchange.getRequestHeaders().getFirst("fi-project-name"),
                    ExportTraceServiceRequest.parseFrom(exchange.getRequestBody())));
                exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
                exchange.sendResponseHeaders(200, -1);
            }
        });
        server.start();
    }

    @AfterEach
    void cleanup() {
        TraceAI.shutdown();
        if (server != null) {
            server.stop(0);
        }
        GlobalOpenTelemetry.resetForTest();
    }

    @Test
    void ownedProviderFlushesExactlyOncePerLifetimeWithGlobalParentAndExistingWireContract() {
        try (SdkTracerProvider application = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(InMemorySpanExporter.create())).build()) {
            OpenTelemetrySdk.builder().setTracerProvider(application).buildAndRegisterGlobal();
            var global = GlobalOpenTelemetry.get();
            Span parent = global.getTracer("application").spanBuilder("parent").startSpan();
            try (Scope ignored = parent.makeCurrent()) {
                for (int lifetime = 0; lifetime < 2; lifetime++) {
                    TraceAI.init(TraceConfig.builder().baseUrl(baseUrl() + "/")
                        .apiKey("synthetic-api-key").secretKey("synthetic-secret-key")
                        .projectName("lifecycle-project").serviceName("lifecycle-service")
                        .exportIntervalMs(60000).hideInputs(true).hideOutputs(true)
                        .hideErrorDetails(true).build());
                    FITracer tracer = TraceAI.getTracer();
                    Span span = tracer.startSpan("owned-" + lifetime, FISpanKind.LLM);
                    tracer.setInputValue(span, "private-input");
                    tracer.setOutputValue(span, "private-output");
                    tracer.setTokenCounts(span, 3, 2, 5);
                    tracer.setError(span, new IllegalArgumentException("private-error"));
                    span.end();
                    TraceAI.shutdown();
                    TraceAI.shutdown();
                    assertThat(GlobalOpenTelemetry.get()).isSameAs(global);
                    assertThat(requests).hasSize(lifetime + 1);
                }
            }
            parent.end();
            for (int lifetime = 0; lifetime < 2; lifetime++) {
                Request request = requests.get(lifetime);
                assertThat(request.path()).isEqualTo("/tracer/v1/traces");
                assertThat(request.apiKey()).isEqualTo("synthetic-api-key");
                assertThat(request.secretKey()).isEqualTo("synthetic-secret-key");
                assertThat(request.project()).isEqualTo("lifecycle-project");
                assertThat(request.body().getResourceSpansCount()).isEqualTo(1);
                ResourceSpans resource = request.body().getResourceSpans(0);
                assertThat(attributes(resource.getResource().getAttributesList()).get("service.name").getStringValue())
                    .isEqualTo("lifecycle-service");
                assertThat(attributes(resource.getResource().getAttributesList()).get("project_name").getStringValue())
                    .isEqualTo("lifecycle-project");
                assertThat(attributes(resource.getResource().getAttributesList()).get("project_type").getStringValue())
                    .isEqualTo("observe");
                assertThat(resource.getScopeSpansCount()).isEqualTo(1);
                var scope = resource.getScopeSpans(0);
                assertThat(scope.getScope().getName()).isEqualTo("traceai-java");
                assertThat(scope.getSpansCount()).isEqualTo(1);
                var span = scope.getSpans(0);
                assertThat(span.getName()).isEqualTo("owned-" + lifetime);
                assertThat(java.util.HexFormat.of().formatHex(span.getTraceId().toByteArray()))
                    .isEqualTo(parent.getSpanContext().getTraceId());
                assertThat(java.util.HexFormat.of().formatHex(span.getParentSpanId().toByteArray()))
                    .isEqualTo(parent.getSpanContext().getSpanId());
                var attrs = attributes(span.getAttributesList());
                assertThat(attrs.get(SemanticConventions.FI_SPAN_KIND).getStringValue()).isEqualTo("LLM");
                assertThat(attrs.get(SemanticConventions.LLM_TOKEN_COUNT_TOTAL).getIntValue()).isEqualTo(5);
                assertThat(span.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
                assertThat(span.getStatus().getMessage()).isEmpty();
                assertThat(request.body().toString()).doesNotContain("private-input", "private-output", "private-error");
            }
        }
    }

    @Test
    void borrowedPipelineExportsOnceAndRemainsUsableAfterTraceAIShutdown() {
        var exporter = OtlpHttpSpanExporter.builder().setEndpoint(baseUrl() + "/tracer/v1/traces")
            .addHeader("X-Api-Key", "application-key").build();
        try (SdkTracerProvider application = SdkTracerProvider.builder()
                .setResource(Resource.create(Attributes.of(stringKey("service.name"), "application-service")))
                .addSpanProcessor(BatchSpanProcessor.builder(exporter).setScheduleDelay(1, TimeUnit.DAYS).build())
                .build()) {
            var sdk = OpenTelemetrySdk.builder().setTracerProvider(application).build();
            TraceAI.init(TraceConfig.builder().baseUrl(baseUrl()).apiKey("must-not-create-exporter")
                .projectName("must-not-replace-resource").build(), sdk);
            TraceAI.getTracer().startSpan("borrowed", FISpanKind.CHAIN).end();
            TraceAI.shutdown();
            // Detaching does not force a batch flush or close the application provider.
            assertThat(requests).isEmpty();
            sdk.getTracer("application").spanBuilder("still-running").startSpan().end();
            assertThat(application.forceFlush().join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
            assertThat(requests).hasSize(1);
            Request request = requests.get(0);
            assertThat(request.apiKey()).isEqualTo("application-key");
            assertThat(request.secretKey()).isNull();
            assertThat(request.project()).isNull();
            var resources = request.body().getResourceSpansList();
            assertThat(resources).hasSize(1);
            var resource = resources.get(0);
            assertThat(attributes(resource.getResource().getAttributesList()).get("service.name").getStringValue())
                .isEqualTo("application-service");
            assertThat(attributes(resource.getResource().getAttributesList())).doesNotContainKey("project_name");
            var names = resource.getScopeSpansList().stream().flatMap(scope -> scope.getSpansList().stream())
                .map(io.opentelemetry.proto.trace.v1.Span::getName).collect(Collectors.toList());
            assertThat(names).containsExactlyInAnyOrder("borrowed", "still-running");
        }
        assertThat(requests).hasSize(1);
    }

    private String baseUrl() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    private static Map<String, AnyValue> attributes(List<KeyValue> values) {
        return values.stream().collect(Collectors.toMap(KeyValue::getKey, KeyValue::getValue));
    }

    private record Request(String path, String apiKey, String secretKey, String project,
                           ExportTraceServiceRequest body) {}
}
