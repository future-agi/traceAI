package ai.traceai.spring;

import io.micrometer.common.KeyValue;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationRegistry;
import io.micrometer.tracing.otel.bridge.OtelCurrentTraceContext;
import io.micrometer.tracing.otel.bridge.OtelSpan;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.observation.EmbeddingModelObservationContext;
import org.springframework.ai.tool.definition.ToolDefinition;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class SpringAITracingObservationHandlerTest {
    private static final String SECRET = "synthetic-private-error-31";

    static Stream<Observation.Context> aiContexts() {
        return Stream.of(chat(), EmbeddingModelObservationContext.builder()
            .embeddingRequest(new EmbeddingRequest(List.of("input"), null)).provider("synthetic").build(),
            ToolCallingObservationContext.builder().toolDefinition(ToolDefinition.builder()
                .name("synthetic-tool").description("Synthetic test tool").inputSchema("{}").build()).build());
    }

    @ParameterizedTest @MethodSource("aiContexts")
    void errorsArePrivateBeforeStopAndKeepOriginalCauseAndType(Observation.Context context) {
        try (var fixture = new Fixture(true)) {
            var error = new IllegalStateException(SECRET, new RuntimeException(SECRET + "-cause"));
            error.addSuppressed(new IllegalArgumentException(SECRET + "-suppressed"));
            var observation = Observation.start("synthetic", () -> context, fixture.registry);
            observation.error(error);
            var recording = ((ReadableSpan) OtelSpan.toOtel(fixture.handler.getRequiredSpan(context))).toSpanData();
            assertThat(recording.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(recording.getStatus().getDescription()).isEmpty();
            assertThat(recording.getAttributes().get(AttributeKey.stringKey("error.type")))
                .isEqualTo(error.getClass().getName());
            assertThat(recording.getEvents()).singleElement().satisfies(event -> {
                assertThat(event.getName()).isEqualTo("exception");
                assertThat(event.getAttributes().size()).isEqualTo(1);
                assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.type")))
                    .isEqualTo(error.getClass().getName());
            });
            assertThat(context.getError()).isSameAs(error);
            assertThat(error.getMessage()).isEqualTo(SECRET);
            assertThat(error.getCause().getMessage()).isEqualTo(SECRET + "-cause");
            // A custom convention must not reintroduce error details while the handler tags on stop.
            for (String key : List.of("error", "ERROR", "error.message", "exception.message", "exception.stacktrace")) {
                context.addHighCardinalityKeyValue(KeyValue.of(key, SECRET));
            }
            observation.stop();
            var finished = fixture.exporter.getFinishedSpanItems();
            assertThat(finished).hasSize(1);
            assertThat(finished.toString()).doesNotContain(SECRET);
            assertThat(finished.get(0).getEvents()).hasSize(1);
            assertThat(finished.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        }
    }

    @ParameterizedTest @MethodSource("aiContexts")
    void explicitRawErrorPolicyPreservesNativeDetailsAndApplicationException(Observation.Context context) {
        try (var fixture = new Fixture(false)) {
            var error = new IllegalStateException(SECRET);
            assertThatThrownBy(() -> Observation.createNotStarted("synthetic", () -> context, fixture.registry)
                .observe(() -> { throw error; })).isSameAs(error);
            var finished = fixture.exporter.getFinishedSpanItems();
            assertThat(finished).hasSize(1);
            assertThat(finished.get(0).getStatus().getDescription()).contains(SECRET);
            assertThat(finished.get(0).getEvents().toString()).contains(SECRET);
        }
    }

    @Test void genericObservationsKeepDefaultHandlerBehavior() {
        try (var fixture = new Fixture(true)) {
            Observation.start("generic", fixture.registry).error(new RuntimeException(SECRET)).stop();
            assertThat(fixture.exporter.getFinishedSpanItems().get(0).getStatus().getDescription()).contains(SECRET);
        }
    }

    @Test void responseLessChatIsUnknownAndCompletedChatIsSuccessful() {
        try (var fixture = new Fixture(true)) {
            Observation.start("incomplete", SpringAITracingObservationHandlerTest::chat, fixture.registry).stop();
            var completed = chat();
            completed.setResponse(SpanTestSupport.response("answer"));
            Observation.start("completed", () -> completed, fixture.registry).stop();
            var spans = fixture.exporter.getFinishedSpanItems();
            assertThat(spans).hasSize(2);
            assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
            assertThat(spans.get(0).getEvents()).isEmpty();
            assertThat(spans.get(1).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        }
    }

    private static ChatModelObservationContext chat() {
        return ChatModelObservationContext.builder().prompt(new Prompt("input")).provider("synthetic").build();
    }

    private static final class Fixture implements AutoCloseable {
        final InMemorySpanExporter exporter = InMemorySpanExporter.create();
        final SdkTracerProvider sdk = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        final ObservationRegistry registry = ObservationRegistry.create();
        final SpringAITracingObservationHandler handler;
        Fixture(boolean hideErrorDetails) {
            var tracer = new OtelTracer(sdk.get("synthetic"), new OtelCurrentTraceContext(), event -> {});
            handler = new SpringAITracingObservationHandler(tracer, hideErrorDetails);
            registry.observationConfig().observationHandler(handler).observationFilter(new SpringAIObservationFilter());
        }
        @Override public void close() { sdk.close(); }
    }
}
