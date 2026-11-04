package ai.traceai.spring;

import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationRegistry;
import io.micrometer.tracing.otel.bridge.OtelCurrentTraceContext;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.tool.definition.ToolDefinition;
import org.springframework.ai.tool.execution.ToolExecutionException;
import org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;
import org.springframework.beans.factory.support.DefaultListableBeanFactory;
import static org.assertj.core.api.Assertions.*;

class SpringAIToolExecutionExceptionProcessorTest {
    private static final String SECRET = "private-tool-error-1749";
    private static final ToolDefinition TOOL = ToolDefinition.builder().name("weather")
        .description("Synthetic tool").inputSchema("{}").build();

    @ParameterizedTest @ValueSource(booleans = {true, false})
    void recordsOriginalCauseBeforeDelegatingWithoutChangingRecovery(boolean hideDetails) {
        try (var f = new Fixture(hideDetails)) {
            var cause = new IllegalStateException(SECRET);
            var exception = new ToolExecutionException(TOOL, cause);
            var calls = new AtomicInteger();
            var context = tool();
            String result = new String("unchanged result");
            var processor = new SpringAIToolExecutionExceptionProcessor(f.registry, ex -> {
                calls.incrementAndGet();
                assertThat(ex).isSameAs(exception);
                assertThat(context.getError()).isSameAs(cause);
                return result;
            });
            var observation = Observation.createNotStarted("tool", () -> context, f.registry);
            assertThat(observation.observe(() -> processor.process(exception))).isSameAs(result);
            assertThat(calls.get()).isEqualTo(1);
            assertThat(cause.getMessage()).isEqualTo(SECRET);
            assertThat(exception.getCause()).isSameAs(cause);
            assertFailure(f, hideDetails);
        }
    }

    @ParameterizedTest @ValueSource(booleans = {true, false})
    void preservesRethrownExceptionAndRecordsOnlyOneEvent(boolean hideDetails) {
        for (boolean unwrap : new boolean[] {false, true}) {
            try (var f = new Fixture(hideDetails)) {
                var cause = new IllegalStateException(SECRET);
                var exception = new ToolExecutionException(TOOL, cause);
                RuntimeException thrown = unwrap ? cause : exception;
                var processor = new SpringAIToolExecutionExceptionProcessor(f.registry, ex -> { throw thrown; });
                var observation = Observation.createNotStarted("tool", SpringAIToolExecutionExceptionProcessorTest::tool, f.registry);
                assertThatThrownBy(() -> observation.observe(() -> processor.process(exception))).isSameAs(thrown);
                assertFailure(f, hideDetails);
            }
        }
    }

    @Test void repeatedDecorationDoesNotDuplicateEventsOrSpans() {
        try (var f = new Fixture(true)) {
            var calls = new AtomicInteger();
            ToolExecutionExceptionProcessor processor = new SpringAIToolExecutionExceptionProcessor(f.registry,
                new SpringAIToolExecutionExceptionProcessor(f.registry, ex -> { calls.incrementAndGet(); return "recovered"; }));
            Observation.createNotStarted("tool", SpringAIToolExecutionExceptionProcessorTest::tool, f.registry)
                .observe(() -> processor.process(new ToolExecutionException(TOOL, new IllegalStateException(SECRET))));
            assertThat(calls.get()).isEqualTo(1);
            assertFailure(f, true);
        }
    }

    @Test void noObservationAndNoopRegistryDoNotChangePolicyOrCreateSpans() {
        try (var f = new Fixture(true)) {
            var calls = new AtomicInteger();
            var exception = new ToolExecutionException(TOOL, new IllegalStateException(SECRET));
            for (var registry : new ObservationRegistry[] {f.registry, ObservationRegistry.NOOP}) {
                var processor = new SpringAIToolExecutionExceptionProcessor(registry, ex -> {
                    assertThat(ex).isSameAs(exception);
                    calls.incrementAndGet();
                    return "same";
                });
                assertThat(processor.process(exception)).isEqualTo("same");
            }
            assertThat(calls.get()).isEqualTo(2);
            assertThat(f.exporter.getFinishedSpanItems()).isEmpty();
        }
    }

    @Test void neverMarksANonToolCurrentObservationOrItsToolAncestor() {
        try (var f = new Fixture(true)) {
            var parent = Observation.start("parent-tool", SpringAIToolExecutionExceptionProcessorTest::tool, f.registry);
            var child = new Observation.Context();
            var processor = new SpringAIToolExecutionExceptionProcessor(f.registry, ex -> "recovered");
            try (var scope = parent.openScope()) {
                Observation.createNotStarted("non-tool", () -> child, f.registry).observe(() ->
                    processor.process(new ToolExecutionException(TOOL, new IllegalStateException(SECRET))));
            } finally { parent.stop(); }
            assertThat(child.getError()).isNull();
            assertThat(parent.getContext().getError()).isNull();
            assertThat(f.exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
                assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
                assertThat(span.getEvents()).isEmpty();
            });
        }
    }

    @Test void successfulToolResultIsNotInspectedForErrorText() {
        try (var f = new Fixture(true)) {
            var context = tool();
            Observation.createNotStarted("tool", () -> context, f.registry)
                .observe(() -> context.setToolCallResult("Exception: failed tool"));
            var span = f.exporter.getFinishedSpanItems().get(0);
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getEvents()).isEmpty();
        }
    }

    @Test void beanDecorationIsLazyAndIdempotentAndKeepsCustomPolicy() {
        try (var f = new Fixture(true)) {
            var beans = new DefaultListableBeanFactory();
            var postProcessor = SpringAIToolExecutionExceptionProcessor.beanPostProcessor(
                beans.getBeanProvider(ObservationRegistry.class));
            ToolExecutionExceptionProcessor delegate = ex -> "custom policy";
            var wrapped = (ToolExecutionExceptionProcessor) postProcessor.postProcessAfterInitialization(delegate, "processor");
            assertThat(wrapped).isInstanceOf(SpringAIToolExecutionExceptionProcessor.class);
            assertThat(postProcessor.postProcessAfterInitialization(wrapped, "processor")).isSameAs(wrapped);
            var unrelated = new Object();
            assertThat(postProcessor.postProcessAfterInitialization(unrelated, "other")).isSameAs(unrelated);
            // Register after decoration: eager lookup would capture NOOP and miss this native span.
            beans.registerSingleton("observationRegistry", f.registry);
            assertThat(Observation.createNotStarted("tool", SpringAIToolExecutionExceptionProcessorTest::tool, f.registry)
                .observe(() -> wrapped.process(new ToolExecutionException(TOOL, new IllegalStateException(SECRET)))))
                .isEqualTo("custom policy");
            assertFailure(f, true);
        }
    }

    private static ToolCallingObservationContext tool() {
        return ToolCallingObservationContext.builder().toolDefinition(TOOL).build();
    }

    private static void assertFailure(Fixture f, boolean hidden) {
        assertThat(f.exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(AttributeKey.stringKey("error.type")))
                .isEqualTo(IllegalStateException.class.getName());
            assertThat(span.getEvents()).singleElement().satisfies(event -> {
                assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.type")))
                    .isEqualTo(IllegalStateException.class.getName());
                if (hidden) assertThat(event.getAttributes().size()).isEqualTo(1);
                else assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.message"))).isEqualTo(SECRET);
            });
            if (hidden) {
                assertThat(span.getStatus().getDescription()).isEmpty();
                assertThat(span.toString()).doesNotContain(SECRET);
            } else assertThat(span.getStatus().getDescription()).contains(SECRET);
        });
    }

    private static final class Fixture implements AutoCloseable {
        final InMemorySpanExporter exporter = InMemorySpanExporter.create();
        final SdkTracerProvider sdk = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        final ObservationRegistry registry = ObservationRegistry.create();
        Fixture(boolean hideDetails) {
            var tracer = new OtelTracer(sdk.get("synthetic"), new OtelCurrentTraceContext(), event -> {});
            registry.observationConfig().observationHandler(new SpringAITracingObservationHandler(tracer, hideDetails))
                .observationFilter(new SpringAIObservationFilter());
        }
        @Override public void close() { sdk.close(); }
    }
}
