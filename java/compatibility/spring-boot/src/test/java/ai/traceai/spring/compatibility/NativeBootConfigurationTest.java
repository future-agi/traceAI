package ai.traceai.spring.compatibility;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationRegistry;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Configuration;

import java.net.InetSocketAddress;
import java.util.HexFormat;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

class NativeBootConfigurationTest {
    @AfterEach
    void reset() {
        TraceAI.shutdown();
    }

    @Test
    void actualBootAutoConfigurationOwnsOneProviderAndConnectsMicrometerParent() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        new ApplicationContextRunner().withUserConfiguration(Application.class)
            .withBean(InMemorySpanExporter.class, () -> exporter)
            .withPropertyValues("management.tracing.sampling.probability=1.0")
            .run(context -> {
                assertThat(context).hasNotFailed().hasSingleBean(FITracer.class)
                    .hasSingleBean(OpenTelemetry.class).hasSingleBean(SdkTracerProvider.class);
                FITracer tracer = context.getBean(FITracer.class);
                assertThat(tracer).isSameAs(TraceAI.getTracer());
                Observation.createNotStarted("native-parent", context.getBean(ObservationRegistry.class))
                    .observe(() -> tracer.startSpan("fi-child", FISpanKind.CHAIN).end());
                assertThat(context.getBean(SdkTracerProvider.class).forceFlush()
                    .join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
                assertThat(exporter.getFinishedSpanItems()).extracting(SpanData::getName)
                    .containsExactlyInAnyOrder("native-parent", "fi-child");
                SpanData child = exporter.getFinishedSpanItems().stream()
                    .filter(span -> span.getName().equals("fi-child")).findFirst().orElseThrow();
                SpanData parent = exporter.getFinishedSpanItems().stream()
                    .filter(span -> span.getName().equals("native-parent")).findFirst().orElseThrow();
                assertThat(child.getTraceId()).isEqualTo(parent.getTraceId());
                assertThat(child.getParentSpanId()).isEqualTo(parent.getSpanId());
            });
        assertThat(TraceAI.isInitialized()).isFalse();
    }

    @Test
    void configuredStarterExportsToFutureAgiWhileBootKeepsItsOwnSpans() throws Exception {
        List<ExportTraceServiceRequest> received = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                received.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody()));
                exchange.sendResponseHeaders(200, -1);
            }
        });
        server.start();
        try {
            InMemorySpanExporter bootExporter = InMemorySpanExporter.create();
            AtomicReference<SpanData> nativeParent = new AtomicReference<>();
            new ApplicationContextRunner().withUserConfiguration(Application.class)
                .withBean(InMemorySpanExporter.class, () -> bootExporter)
                .withPropertyValues("management.tracing.sampling.probability=1.0",
                    "traceai.base-url=http://127.0.0.1:" + server.getAddress().getPort(),
                    "traceai.api-key=synthetic-key", "traceai.export-interval-ms=86400000")
                .run(context -> {
                    assertThat(context).hasNotFailed().hasSingleBean(SdkTracerProvider.class);
                    FITracer tracer = context.getBean(FITracer.class);
                    Observation.createNotStarted("native-parent", context.getBean(ObservationRegistry.class))
                        .observe(() -> tracer.startSpan("fi-child", FISpanKind.CHAIN).end());
                    assertThat(context.getBean(SdkTracerProvider.class).forceFlush()
                        .join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
                    assertThat(bootExporter.getFinishedSpanItems()).extracting(SpanData::getName)
                        .containsExactly("native-parent");
                    // The in-memory exporter is cleared when Boot closes its provider.
                    nativeParent.set(bootExporter.getFinishedSpanItems().get(0));
                });
            assertThat(received).hasSize(1);
            var spans = received.get(0).getResourceSpansList().stream()
                .flatMap(resource -> resource.getScopeSpansList().stream())
                .flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).extracting(io.opentelemetry.proto.trace.v1.Span::getName).containsExactly("fi-child");
            SpanData parent = nativeParent.get();
            assertThat(HexFormat.of().formatHex(spans.get(0).getTraceId().toByteArray())).isEqualTo(parent.getTraceId());
            assertThat(HexFormat.of().formatHex(spans.get(0).getParentSpanId().toByteArray())).isEqualTo(parent.getSpanId());
        } finally {
            server.stop(0);
        }
        assertThat(TraceAI.isInitialized()).isFalse();
    }

    @Test
    void documentedBootOtlpSetupCarriesFiTracerSpansWithPrivacy() throws Exception {
        List<ExportTraceServiceRequest> received = new CopyOnWriteArrayList<>();
        List<String> apiKeys = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try (exchange) {
                apiKeys.add(exchange.getRequestHeaders().getFirst("X-Api-Key"));
                received.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody()));
                exchange.sendResponseHeaders(200, -1);
            }
        });
        server.start();
        String endpoint = "http://127.0.0.1:" + server.getAddress().getPort() + "/tracer/v1/traces";
        try {
            new ApplicationContextRunner().withUserConfiguration(Application.class)
                // Boot 3 and Boot 4 property names; each Boot line ignores the other's.
                .withPropertyValues("management.tracing.sampling.probability=1.0",
                    "management.otlp.tracing.endpoint=" + endpoint,
                    "management.otlp.tracing.headers.X-Api-Key=synthetic-key",
                    "management.opentelemetry.tracing.export.otlp.endpoint=" + endpoint,
                    "management.opentelemetry.tracing.export.otlp.headers.X-Api-Key=synthetic-key",
                    "management.opentelemetry.resource-attributes.project_name=boot-native-project",
                    "traceai.hide-inputs=true")
                .run(context -> {
                    assertThat(context).hasNotFailed().hasSingleBean(SdkTracerProvider.class);
                    FITracer tracer = context.getBean(FITracer.class);
                    Observation.createNotStarted("native-parent", context.getBean(ObservationRegistry.class))
                        .observe(() -> {
                            var span = tracer.startSpan("fi-child", FISpanKind.LLM);
                            tracer.setInputValue(span, "private-input");
                            span.end();
                        });
                });
            // Boot flushes its own provider when the context closes.
            assertThat(apiKeys).isNotEmpty().containsOnly("synthetic-key");
            var resources = received.stream().flatMap(request -> request.getResourceSpansList().stream()).toList();
            assertThat(resources).allSatisfy(resource -> assertThat(resource.getResource().getAttributesList())
                .anySatisfy(attribute -> {
                    assertThat(attribute.getKey()).isEqualTo("project_name");
                    assertThat(attribute.getValue().getStringValue()).isEqualTo("boot-native-project");
                }));
            var spans = resources.stream().flatMap(resource -> resource.getScopeSpansList().stream())
                .flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).extracting(io.opentelemetry.proto.trace.v1.Span::getName)
                .containsExactlyInAnyOrder("native-parent", "fi-child");
            var parent = spans.stream().filter(span -> span.getName().equals("native-parent")).findFirst().orElseThrow();
            var child = spans.stream().filter(span -> span.getName().equals("fi-child")).findFirst().orElseThrow();
            assertThat(child.getTraceId()).isEqualTo(parent.getTraceId());
            assertThat(child.getParentSpanId()).isEqualTo(parent.getSpanId());
            assertThat(received.toString()).doesNotContain("private-input");
        } finally {
            server.stop(0);
        }
        assertThat(TraceAI.isInitialized()).isFalse();
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    static class Application {}
}
