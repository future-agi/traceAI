package ai.traceai;

import static org.assertj.core.api.Assertions.assertThat;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

/**
 * The reranker document keys hold a whole-number document count in the Java instrumentations (Cohere rerank
 * writes how many documents went in and came out). A count is not content, so it stays visible under every
 * hide setting; the documents themselves (text on these keys, or keys below them) are still masked.
 */
class RerankerDocumentCountsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    static final List<String> COUNT_KEYS = List.of(
        SemanticConventions.GEN_AI_RERANKER_INPUT_DOCUMENTS, SemanticConventions.GEN_AI_RERANKER_OUTPUT_DOCUMENTS,
        "reranker.input_documents", "reranker.output_documents");

    static Stream<TraceConfig> configs() {
        return Stream.of(
            TraceConfig.builder().hideInputs(true).build(),
            TraceConfig.builder().hideOutputs(true).build(),
            TraceConfig.builder().hideInputMessages(true).build(),
            TraceConfig.builder().hideOutputMessages(true).build(),
            TraceConfig.builder().hideErrorDetails(true).build(),
            TraceConfig.builder().hideInputs(true).hideOutputs(true).hideInputMessages(true)
                .hideOutputMessages(true).hideErrorDetails(true).build());
    }

    private static Attributes write(TraceConfig config, java.util.function.BiConsumer<FITracer, Span> body) {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("counts"), config);
        Span span = tracer.startSpan("rerank", FISpanKind.RERANKER);
        body.accept(tracer, span);
        span.end();
        return otel.getSpans().get(0).getAttributes();
    }

    private static String flags(TraceConfig c) {
        return "hideIn=" + c.isHideInputs() + " hideOut=" + c.isHideOutputs() + " inMsg=" + c.isHideInputMessages()
            + " outMsg=" + c.isHideOutputMessages() + " err=" + c.isHideErrorDetails();
    }

    @ParameterizedTest
    @MethodSource("configs")
    void documentCountsStayVisibleUnderEveryHideSetting(TraceConfig config) {
        for (String key : COUNT_KEYS) {
            Attributes exported = write(config, (tracer, span) -> tracer.setAttribute(span, key, 3L));
            assertThat(exported.get(AttributeKey.longKey(key))).as(key + " " + flags(config)).isEqualTo(3L);
            assertThat(config.maskTypedAttribute(key, 3L)).as(key + " " + flags(config)).isEqualTo(3L);
        }
    }

    @ParameterizedTest
    @MethodSource("configs")
    void documentTextOnTheSameKeysFollowsTheHideSettings(TraceConfig config) {
        for (String key : COUNT_KEYS) {
            boolean input = key.contains("input_documents");
            boolean hidden = input ? config.isHideInputs() : config.isHideOutputs();
            String child = key + ".0.document.content";
            Attributes exported = write(config, (tracer, span) -> {
                tracer.setAttribute(span, key, "private-document");
                tracer.setAttribute(span, child, "private-document");
                tracer.setAttribute(span, child + "_length", 16L);
            });
            String expected = hidden ? ContentKeys.REDACTED : "private-document";
            assertThat(exported.get(AttributeKey.stringKey(key))).as(key + " " + flags(config)).isEqualTo(expected);
            assertThat(exported.get(AttributeKey.stringKey(child))).as(child + " " + flags(config)).isEqualTo(expected);
            // Only the exact count keys carry the count rule: a number below them is content like any other.
            if (hidden) {
                assertThat(exported.get(AttributeKey.longKey(child + "_length"))).as(child + "_length").isNull();
            }
        }
    }
}
