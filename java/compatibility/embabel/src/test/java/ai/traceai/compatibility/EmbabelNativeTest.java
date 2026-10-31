package ai.traceai.compatibility;

import ai.traceai.nativeotel.spring.EmbabelTracingConfiguration;
import com.embabel.agent.autoconfigure.observability.OpenTelemetrySdkAutoConfiguration;
import com.embabel.agent.observability.ObservabilityProperties;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.ResourceSpans;
import io.opentelemetry.proto.trace.v1.Span;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.micrometer.tracing.opentelemetry.autoconfigure.OpenTelemetryTracingAutoConfiguration;
import org.springframework.boot.micrometer.tracing.opentelemetry.autoconfigure.otlp.OtlpTracingAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.SystemEnvironmentPropertySource;

import java.net.InetSocketAddress;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/** Real Boot exporter, real Embabel provider, synthetic spans; only loopback HTTP is used. */
class EmbabelNativeTest {
    private static final String PRIVATE_TEXT = "synthetic-private-error";
    private final List<ExportTraceServiceRequest> requests = new CopyOnWriteArrayList<>();
    private final List<Map<String, List<String>>> headers = new CopyOnWriteArrayList<>();
    private final List<String> paths = new CopyOnWriteArrayList<>();
    private HttpServer receiver;

    @BeforeEach
    void start() throws Exception {
        GlobalOpenTelemetry.resetForTest();
        receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/", exchange -> {
            requests.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
            headers.add(exchange.getRequestHeaders().entrySet().stream().collect(Collectors.toMap(
                    e -> e.getKey().toLowerCase(java.util.Locale.ROOT), e -> List.copyOf(e.getValue()))));
            paths.add(exchange.getRequestURI().getPath());
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        receiver.start();
    }

    @AfterEach
    void stop() {
        receiver.stop(0);
        GlobalOpenTelemetry.resetForTest();
    }

    private ApplicationContextRunner baseContext() {
        return baseContext(true);
    }

    private ApplicationContextRunner baseContext(boolean bootProcessor) {
        Class<?>[] configurations = bootProcessor
                ? new Class<?>[]{OpenTelemetrySdkAutoConfiguration.class, OpenTelemetryTracingAutoConfiguration.class,
                        OtlpTracingAutoConfiguration.class}
                : new Class<?>[]{OpenTelemetrySdkAutoConfiguration.class, OtlpTracingAutoConfiguration.class};
        return new ApplicationContextRunner()
                .withConfiguration(AutoConfigurations.of(configurations))
                .withUserConfiguration(EmbabelTracingConfiguration.class)
                .withPropertyValues(
                        "embabel.agent.platform.observability.service-name=native-embabel",
                        "management.tracing.sampling.probability=1.0",
                        "management.opentelemetry.tracing.export.schedule-delay=1h",
                        "management.opentelemetry.tracing.export.otlp.transport=http",
                        "management.opentelemetry.tracing.export.otlp.compression=none",
                        "management.opentelemetry.tracing.export.otlp.headers.X-Api-Key=fixture-api",
                        "management.opentelemetry.tracing.export.otlp.headers.X-Secret-Key=fixture-secret",
                        "management.opentelemetry.tracing.export.otlp.endpoint=http://127.0.0.1:"
                                + receiver.getAddress().getPort() + "/tracer/v1/traces");
    }

    private ApplicationContextRunner context() {
        return baseContext().withPropertyValues("fi.project-name=fixture-project");
    }

    private void emit(ApplicationContextRunner context) {
        context.run(ctx -> {
            assertThat(ctx).hasNotFailed().hasSingleBean(SdkTracerProvider.class).hasSingleBean(OpenTelemetry.class);
            // Boot and Embabel both own native SDK paths and back off to one another.
            assertThat(ctx.containsBean("sdkTracerProvider") || ctx.containsBean("otelSdkTracerProvider")).isTrue();
            assertThat(ctx).doesNotHaveBean(ai.traceai.FITracer.class);
            var span = ctx.getBean(OpenTelemetry.class).getTracer("embabel-contract")
                    .spanBuilder("execute_tool fixture").startSpan();
            span.setStatus(StatusCode.ERROR, PRIVATE_TEXT);
            span.setAttribute("embabel.tool.error.message", PRIVATE_TEXT);
            span.setAttribute("embabel.llm.error.message", PRIVATE_TEXT);
            span.setAttribute("error.message", PRIVATE_TEXT);
            span.setAttribute("embabel.tool.error.type", "ToolReturnedError");
            span.recordException(new IllegalArgumentException(PRIVATE_TEXT));
            span.addEvent("fixture", Attributes.builder().put("embabel.tool.error.message", PRIVATE_TEXT).build());
            span.end();
        }); // Native Spring context shutdown must drain the pending batch (configured delay: 1 hour).
        assertThat(requests).isNotEmpty();
    }

    private List<ResourceSpans> resources() {
        return requests.stream().flatMap(r -> r.getResourceSpansList().stream()).toList();
    }

    private List<Span> spans() {
        return resources().stream().flatMap(r -> r.getScopeSpansList().stream())
                .flatMap(s -> s.getSpansList().stream()).toList();
    }

    private static Map<String, String> attributes(List<KeyValue> values) {
        return values.stream().collect(Collectors.toMap(KeyValue::getKey, v -> v.getValue().getStringValue()));
    }

    private void assertProject(String project, String service) {
        assertThat(resources()).isNotEmpty().allSatisfy(r -> assertThat(attributes(r.getResource().getAttributesList()))
                .containsEntry("project_name", project).containsEntry("project_type", "observe")
                .containsEntry("service.name", service));
    }

    @Test
    void nativeResourceCarriesTheObserveProjectWithOneExportOnShutdown() {
        emit(context());
        assertProject("fixture-project", "native-embabel");
        assertThat(spans()).hasSize(1);
        assertThat(paths).containsExactly("/tracer/v1/traces");
        assertThat(headers).singleElement().satisfies(h -> assertThat(h)
                .containsEntry("x-api-key", List.of("fixture-api"))
                .containsEntry("x-secret-key", List.of("fixture-secret"))
                .containsEntry("content-type", List.of("application/x-protobuf"))
                .doesNotContainKey("content-encoding"));
    }

    @Test
    void frameworkErrorTextIsPrivateOnTheWire() {
        emit(context());
        assertThat(requests.toString()).doesNotContain(PRIVATE_TEXT).doesNotContain("exception.stacktrace");
        assertThat(spans()).hasSize(1);
        assertThat(attributes(spans().getFirst().getAttributesList()))
                .containsEntry("embabel.tool.error.type", "ToolReturnedError");
        assertThat(spans().getFirst().getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
        assertThat(requests.toString()).contains("java.lang.IllegalArgumentException");
    }

    @Test
    void embabelsOwnBatchProcessorAlsoExportsOnceWithPrivacyAndRouting() {
        emit(baseContext(false).withPropertyValues("fi.project-name=fixture-project"));
        assertProject("fixture-project", "native-embabel");
        assertThat(spans()).hasSize(1);
        assertThat(requests.toString()).doesNotContain(PRIVATE_TEXT).doesNotContain("exception.stacktrace");
    }

    @Test
    void privateDefaultsReachEmbabelBeforePropertyBinding() {
        context().run(ctx -> {
            assertThat(ctx).hasNotFailed();
            ObservabilityProperties properties = ctx.getBean(ObservabilityProperties.class);
            assertThat(properties.isCaptureMessageContent()).isFalse();
            assertThat(properties.isTraceHttpDetails()).isFalse();
        });
    }

    @Test
    void contentOptInRetainsMessagesButNeverStackTraces() {
        var optIn = context().withPropertyValues("embabel.agent.platform.observability.capture-message-content=true");
        emit(optIn);
        assertThat(requests.toString()).contains(PRIVATE_TEXT).doesNotContain("exception.stacktrace");
        assertThat(spans().getFirst().getStatus().getMessage()).isEqualTo(PRIVATE_TEXT);
        assertThat(attributes(spans().getFirst().getAttributesList()))
                .containsEntry("embabel.llm.error.message", PRIVATE_TEXT);
    }

    @Test
    void environmentProjectAndContentOptInAreHonored() {
        emit(baseContext().withInitializer(ctx -> ctx.getEnvironment().getPropertySources().addFirst(
                new SystemEnvironmentPropertySource("fixtureEnvironment", Map.of(
                        "FI_PROJECT_NAME", "environment-project",
                        "EMBABEL_AGENT_PLATFORM_OBSERVABILITY_CAPTURE_MESSAGE_CONTENT", "true")))));
        assertProject("environment-project", "native-embabel");
        assertThat(requests.toString()).contains(PRIVATE_TEXT).doesNotContain("exception.stacktrace");
    }

    @Test
    void bootResourceProjectPropertyRemainsSupported() {
        emit(baseContext().withPropertyValues("management.opentelemetry.resource-attributes.project_name=boot-project"));
        assertProject("boot-project", "native-embabel");
    }

    @Test
    void userResourceWinsOverBootFallbackAndKeepsItsAttributes() {
        emit(baseContext().withUserConfiguration(UserResource.class)
                .withPropertyValues("management.opentelemetry.resource-attributes.project_name=boot-project"));
        assertProject("custom-project", "custom-service");
        assertThat(resources()).allSatisfy(r -> assertThat(attributes(r.getResource().getAttributesList()))
                .containsEntry("deployment.environment.name", "fixture"));
    }

    @Test
    void explicitFiProjectOverridesTheUserResource() {
        emit(context().withUserConfiguration(UserResource.class));
        assertProject("fixture-project", "custom-service");
    }

    @Test
    void missingOrBlankProjectFailsInsteadOfSilentlyDroppingTraces() {
        for (String[] properties : List.of(new String[0], new String[]{"fi.project-name= "})) {
            baseContext().withPropertyValues(properties).run(ctx -> assertThat(ctx).hasFailed()
                    .getFailure().hasRootCauseMessage("Set fi.project-name (FI_PROJECT_NAME) to an Observe project"));
        }
        assertThat(requests).isEmpty();
    }

    @Test
    void everyManagedExporterIsFilteredWithoutDuplicatingSpans() {
        RecordingExporter second = new RecordingExporter();
        emit(context().withBean("secondExporter", SpanExporter.class, () -> second));
        assertThat(spans()).hasSize(1);
        assertThat(second.spans).hasSize(1);
        SpanData recorded = second.spans.getFirst();
        assertThat(recorded.getAttributes().toString()).doesNotContain(PRIVATE_TEXT);
        assertThat(recorded.getEvents().toString()).doesNotContain(PRIVATE_TEXT);
        assertThat(recorded.getStatus().getDescription()).isEmpty();
        assertThat(recorded.getSpanId()).isEqualTo(java.util.HexFormat.of().formatHex(spans().getFirst().getSpanId().toByteArray()));
    }

    @Configuration(proxyBeanMethods = false)
    static class UserResource {
        @Bean
        Resource resource() {
            return Resource.create(Attributes.builder().put("project_name", "custom-project")
                    .put("service.name", "custom-service").put("deployment.environment.name", "fixture").build());
        }
    }

    static class RecordingExporter implements SpanExporter {
        final List<SpanData> spans = new CopyOnWriteArrayList<>();
        public CompletableResultCode export(Collection<SpanData> batch) {
            spans.addAll(batch);
            return CompletableResultCode.ofSuccess();
        }
        public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
        public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
    }
}
