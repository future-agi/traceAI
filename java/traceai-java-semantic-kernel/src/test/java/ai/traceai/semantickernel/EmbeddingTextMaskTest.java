package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import reactor.core.publisher.Mono;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/** The embedded text is input content: every input-side hide flag must keep it off the span. */
class EmbeddingTextMaskTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String PRIVATE = "private-embedding-text-DO-NOT-EXPORT";

    private SpanData export(TraceConfig config) {
        otel.clearSpans();
        TextEmbeddingGenerationService embedding = mock(TextEmbeddingGenerationService.class);
        when(embedding.generateEmbeddingsAsync(any())).thenReturn(Mono.just(List.of()));
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
        new TracedTextEmbeddingGenerationService(embedding, "embedding-model", "test", tracer)
            .generateEmbeddingsAsync(List.of(PRIVATE)).block();
        return otel.getSpans().get(0);
    }

    private static boolean exportsText(SpanData data) {
        return data.getAttributes().asMap().values().stream().anyMatch(v -> String.valueOf(v).contains(PRIVATE));
    }

    @Test
    void textIsExportedWhenNothingIsHidden() {
        assertThat(exportsText(export(TraceConfig.builder().build()))).isTrue();
    }

    @Test
    void hideInputsKeepsTheTextOffTheSpan() {
        assertThat(exportsText(export(TraceConfig.builder().hideInputs(true).build()))).isFalse();
    }

    @Test
    void hideInputMessagesKeepsTheTextOffTheSpan() {
        assertThat(exportsText(export(TraceConfig.builder().hideInputMessages(true).build()))).isFalse();
    }

    @Test
    void hideOutputsDoesNotHideTheInputText() {
        assertThat(exportsText(export(TraceConfig.builder().hideOutputs(true).build()))).isTrue();
    }
}
