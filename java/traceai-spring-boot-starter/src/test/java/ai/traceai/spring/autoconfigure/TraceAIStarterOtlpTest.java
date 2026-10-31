package ai.traceai.spring.autoconfigure;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.support.AbstractBeanDefinition;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static io.opentelemetry.api.common.AttributeKey.stringKey;
import static org.assertj.core.api.Assertions.assertThat;

class TraceAIStarterOtlpTest {
    private HttpServer server;
    private final List<Request> requests = new CopyOnWriteArrayList<>();

    @BeforeEach
    void startReceiver() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                requests.add(new Request(exchange.getRequestURI().getPath(),
                    exchange.getRequestHeaders().getFirst("X-Api-Key"),
                    exchange.getRequestHeaders().getFirst("X-Secret-Key"),
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
    }

    @Test
    void standaloneContextCloseFlushesConfiguredPrivateTracesOnce() {
        runner().withPropertyValues("spring.application.name=boot-service", "traceai.project-name=boot-project")
            .run(context -> {
                assertThat(context).hasNotFailed();
                FITracer tracer = context.getBean(FITracer.class);
                Span span = tracer.startSpan("standalone", FISpanKind.LLM);
                tracer.setInputValue(span, "private-input");
                tracer.setOutputValue(span, "private-output");
                tracer.setTokenCounts(span, 2, 1, 3);
                span.end();
                assertThat(requests).isEmpty();
            });
        assertThat(TraceAI.isInitialized()).isFalse();
        assertThat(requests).hasSize(1);
        Request request = requests.get(0);
        assertThat(request.path()).isEqualTo("/tracer/v1/traces");
        assertThat(request.apiKey()).isEqualTo("synthetic-key");
        assertThat(request.secretKey()).isEqualTo("synthetic-secret");
        var resource = request.body().getResourceSpans(0);
        assertThat(attributes(resource.getResource().getAttributesList()).get("service.name").getStringValue())
            .isEqualTo("boot-service");
        assertThat(attributes(resource.getResource().getAttributesList()).get("project_name").getStringValue())
            .isEqualTo("boot-project");
        var scope = resource.getScopeSpans(0);
        assertThat(scope.getScope().getName()).isEqualTo("traceai-java");
        assertThat(scope.getSpansCount()).isEqualTo(1);
        assertThat(scope.getSpans(0).getName()).isEqualTo("standalone");
        assertThat(request.body().toString()).doesNotContain("private-input", "private-output");
    }

    @Test
    void borrowedSdkOwnsRoutingBatchingAndLifecycleWhilePrivacyAndParentageArePreserved() {
        var exporter = OtlpHttpSpanExporter.builder().setEndpoint(baseUrl() + "/native/v1/traces")
            .addHeader("X-Api-Key", "native-key").build();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .setResource(Resource.create(Attributes.of(stringKey("project_name"), "native-project")))
                .addSpanProcessor(BatchSpanProcessor.builder(exporter).setScheduleDelay(1, TimeUnit.DAYS).build())
                .build()) {
            OpenTelemetry sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
            Span parent = sdk.getTracer("application").spanBuilder("parent").startSpan();
            privacyOnlyRunner().withPropertyValues("traceai.project-name=must-not-override")
                .withBean(OpenTelemetry.class, () -> sdk,
                    definition -> ((AbstractBeanDefinition) definition).setDestroyMethodName(""))
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    FITracer tracer = context.getBean(FITracer.class);
                    try (Scope ignored = parent.makeCurrent()) {
                        Span child = tracer.startSpan("child", FISpanKind.CHAIN);
                        tracer.setInputValue(child, "private-input");
                        tracer.setOutputValue(child, "private-output");
                        child.end();
                    }
                });
            assertThat(requests).isEmpty(); // The starter neither flushes nor closes the borrowed SDK.
            parent.end();
            sdk.getTracer("application").spanBuilder("after-close").startSpan().end();
            assertThat(provider.forceFlush().join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
            assertThat(requests).hasSize(1);
            Request request = requests.get(0);
            assertThat(request.path()).isEqualTo("/native/v1/traces");
            assertThat(request.apiKey()).isEqualTo("native-key");
            assertThat(request.secretKey()).isNull();
            var resource = request.body().getResourceSpans(0);
            assertThat(attributes(resource.getResource().getAttributesList()).get("project_name").getStringValue())
                .isEqualTo("native-project");
            var spans = resource.getScopeSpansList().stream().flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).extracting(io.opentelemetry.proto.trace.v1.Span::getName)
                .containsExactlyInAnyOrder("parent", "child", "after-close");
            var child = spans.stream().filter(span -> span.getName().equals("child")).findFirst().orElseThrow();
            assertThat(HexFormat.of().formatHex(child.getTraceId().toByteArray())).isEqualTo(parent.getSpanContext().getTraceId());
            assertThat(HexFormat.of().formatHex(child.getParentSpanId().toByteArray())).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(request.body().toString()).doesNotContain("private-input", "private-output", "must-not-override");
        }
        assertThat(requests).hasSize(1);
    }

    @Test
    void configuredStarterKeepsFutureAgiExportBesideApplicationSdk() {
        InMemorySpanExporter applicationExporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(applicationExporter)).build()) {
            OpenTelemetry sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
            Span parent = sdk.getTracer("application").spanBuilder("application-parent").startSpan();
            runner().withPropertyValues("traceai.project-name=boot-project")
                .withBean(OpenTelemetry.class, () -> sdk,
                    definition -> ((AbstractBeanDefinition) definition).setDestroyMethodName(""))
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    FITracer tracer = context.getBean(FITracer.class);
                    try (Scope ignored = parent.makeCurrent()) {
                        Span child = tracer.startSpan("configured-child", FISpanKind.LLM);
                        tracer.setInputValue(child, "private-input");
                        child.end();
                    }
                });
            parent.end();
            // Exactly one pipeline per span: FITracer to Future AGI, application spans to the application.
            assertThat(applicationExporter.getFinishedSpanItems()).extracting(SpanData::getName)
                .containsExactly("application-parent");
            assertThat(requests).hasSize(1);
            Request request = requests.get(0);
            assertThat(request.path()).isEqualTo("/tracer/v1/traces");
            assertThat(request.apiKey()).isEqualTo("synthetic-key");
            var resource = request.body().getResourceSpans(0);
            assertThat(attributes(resource.getResource().getAttributesList()).get("project_name").getStringValue())
                .isEqualTo("boot-project");
            var spans = resource.getScopeSpansList().stream().flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).extracting(io.opentelemetry.proto.trace.v1.Span::getName).containsExactly("configured-child");
            assertThat(HexFormat.of().formatHex(spans.get(0).getTraceId().toByteArray()))
                .isEqualTo(parent.getSpanContext().getTraceId());
            assertThat(HexFormat.of().formatHex(spans.get(0).getParentSpanId().toByteArray()))
                .isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(request.body().toString()).doesNotContain("private-input");
        }
    }

    private ApplicationContextRunner runner() {
        return privacyOnlyRunner().withPropertyValues("traceai.base-url=" + baseUrl(),
            "traceai.api-key=synthetic-key", "traceai.secret-key=synthetic-secret",
            "traceai.export-interval-ms=86400000");
    }

    private ApplicationContextRunner privacyOnlyRunner() {
        return new ApplicationContextRunner().withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class))
            .withPropertyValues("traceai.hide-inputs=true", "traceai.hide-outputs=true");
    }

    private String baseUrl() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    private static Map<String, AnyValue> attributes(List<KeyValue> values) {
        return values.stream().collect(Collectors.toMap(KeyValue::getKey, KeyValue::getValue));
    }

    private record Request(String path, String apiKey, String secretKey, ExportTraceServiceRequest body) {}
}
