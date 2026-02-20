package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.microsoft.semantickernel.services.textembedding.Embedding;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import reactor.core.publisher.Mono;

import java.util.Arrays;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TracedTextEmbeddingGenerationServiceTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private TextEmbeddingGenerationService delegate;

    private FITracer tracer;
    private TracedTextEmbeddingGenerationService tracedService;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
        tracedService = new TracedTextEmbeddingGenerationService(
            delegate, "text-embedding-ada-002", "openai", tracer
        );
    }

    @Test
    void shouldSetCorrectSpanKindForEmbedding() {
        // Given
        List<String> texts = Arrays.asList("Hello", "World");
        Embedding embedding1 = new Embedding(Arrays.asList(0.1f, 0.2f, 0.3f));
        Embedding embedding2 = new Embedding(Arrays.asList(0.4f, 0.5f, 0.6f));

        when(delegate.generateEmbeddingsAsync(any()))
            .thenReturn(Mono.just(Arrays.asList(embedding1, embedding2)));

        // When
        tracedService.generateEmbeddingsAsync(texts).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.EMBEDDING.getValue());
    }

    @Test
    void shouldSetEmbeddingAttributes() {
        // Given
        List<String> texts = Arrays.asList("Test text");
        Embedding embedding = new Embedding(Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f, 0.5f));

        when(delegate.generateEmbeddingsAsync(any()))
            .thenReturn(Mono.just(Arrays.asList(embedding)));

        // When
        tracedService.generateEmbeddingsAsync(texts).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // LLM_SYSTEM and LLM_PROVIDER both map to "gen_ai.provider.name",
        // so the provider value ("openai") overwrites the system value
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)
        )).isEqualTo("openai");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.EMBEDDING_MODEL_NAME)
        )).isEqualTo("text-embedding-ada-002");
    }

    @Test
    void shouldCaptureVectorCountAndDimensions() {
        // Given
        List<String> texts = Arrays.asList("Text 1", "Text 2", "Text 3");
        List<Float> vector = Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f);
        Embedding embedding1 = new Embedding(vector);
        Embedding embedding2 = new Embedding(vector);
        Embedding embedding3 = new Embedding(vector);

        when(delegate.generateEmbeddingsAsync(any()))
            .thenReturn(Mono.just(Arrays.asList(embedding1, embedding2, embedding3)));

        // When
        tracedService.generateEmbeddingsAsync(texts).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT)
        )).isEqualTo(3L);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)
        )).isEqualTo(4L);
    }

    @Test
    void shouldCaptureInputTexts() {
        // Given
        List<String> texts = Arrays.asList("Hello", "World");
        Embedding embedding = new Embedding(Arrays.asList(0.1f, 0.2f, 0.3f));

        when(delegate.generateEmbeddingsAsync(any()))
            .thenReturn(Mono.just(Arrays.asList(embedding, embedding)));

        // When
        tracedService.generateEmbeddingsAsync(texts).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey("embedding.input_count")
        )).isEqualTo(2L);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.EMBEDDING_INPUT_TEXT + ".0")
        )).isEqualTo("Hello");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.EMBEDDING_INPUT_TEXT + ".1")
        )).isEqualTo("World");
    }

    @Test
    void shouldHandleErrors() {
        // Given
        List<String> texts = Arrays.asList("Test");
        RuntimeException error = new RuntimeException("Embedding API Error");

        when(delegate.generateEmbeddingsAsync(any()))
            .thenReturn(Mono.error(error));

        // When/Then
        try {
            tracedService.generateEmbeddingsAsync(texts).block();
        } catch (Exception e) {
            // Expected
        }

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldUnwrapToOriginalService() {
        assertThat(tracedService.unwrap()).isSameAs(delegate);
    }

    @Test
    void shouldReturnTracer() {
        assertThat(tracedService.getTracer()).isSameAs(tracer);
    }

    @Test
    void shouldDelegateGetModelId() {
        when(delegate.getModelId()).thenReturn("text-embedding-ada-002");
        assertThat(tracedService.getModelId()).isEqualTo("text-embedding-ada-002");
    }

    @Test
    void shouldDelegateGetServiceId() {
        when(delegate.getServiceId()).thenReturn("embedding-service");
        assertThat(tracedService.getServiceId()).isEqualTo("embedding-service");
    }

    @Test
    void shouldSetCorrectSpanName() {
        // Given
        List<String> texts = Arrays.asList("Test");
        Embedding embedding = new Embedding(Arrays.asList(0.1f, 0.2f, 0.3f));

        when(delegate.generateEmbeddingsAsync(any()))
            .thenReturn(Mono.just(Arrays.asList(embedding)));

        // When
        tracedService.generateEmbeddingsAsync(texts).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans.get(0).getName()).isEqualTo("Semantic Kernel Embedding Generation");
    }
}
