package ai.traceai.ollama;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.github.ollama4j.OllamaAPI;
import io.github.ollama4j.models.OllamaResult;
import io.github.ollama4j.models.chat.OllamaChatMessage;
import io.github.ollama4j.models.chat.OllamaChatMessageRole;
import io.github.ollama4j.models.chat.OllamaChatResult;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedOllamaAPI.
 */
@ExtendWith(MockitoExtension.class)
class TracedOllamaAPITest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private OllamaAPI mockApi;

    @Mock
    private OllamaResult mockResult;

    @Mock
    private OllamaChatResult mockChatResult;

    private FITracer tracer;
    private TracedOllamaAPI tracedApi;

    private static final String MODEL_NAME = "llama3";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedApi = new TracedOllamaAPI(mockApi, tracer);
    }

    // ========== generate Tests ==========

    @Test
    void shouldCreateSpanForGenerate() throws Exception {
        // Arrange
        when(mockApi.generate(MODEL_NAME, "Hello!", false, null)).thenReturn(mockResult);
        when(mockResult.getResponse()).thenReturn("Hi there!");
        when(mockResult.getResponseTime()).thenReturn(0L);

        // Act
        OllamaResult result = tracedApi.generate(MODEL_NAME, "Hello!");

        // Assert
        assertThat(result).isSameAs(mockResult);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Ollama Generate");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("ollama");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetModelAttributesForGenerate() throws Exception {
        // Arrange
        when(mockApi.generate(MODEL_NAME, "Test", false, null)).thenReturn(mockResult);
        when(mockResult.getResponse()).thenReturn("Response");
        when(mockResult.getResponseTime()).thenReturn(0L);

        // Act
        tracedApi.generate(MODEL_NAME, "Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo(MODEL_NAME);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)
        )).isEqualTo(MODEL_NAME);
    }

    @Test
    void shouldCaptureOutputValueForGenerate() throws Exception {
        // Arrange
        String expectedOutput = "This is the generated response";
        when(mockApi.generate(MODEL_NAME, "Hello!", false, null)).thenReturn(mockResult);
        when(mockResult.getResponse()).thenReturn(expectedOutput);
        when(mockResult.getResponseTime()).thenReturn(0L);

        // Act
        tracedApi.generate(MODEL_NAME, "Hello!");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);
    }

    @Test
    void shouldCaptureInputMessagesForGenerate() throws Exception {
        // Arrange
        when(mockApi.generate(MODEL_NAME, "Hello!", false, null)).thenReturn(mockResult);
        when(mockResult.getResponse()).thenReturn("Response");
        when(mockResult.getResponseTime()).thenReturn(0L);

        // Act
        tracedApi.generate(MODEL_NAME, "Hello!");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("Hello!");
    }

    @Test
    void shouldRecordErrorOnGenerateFailure() throws Exception {
        // Arrange
        when(mockApi.generate(MODEL_NAME, "Test", false, null))
            .thenThrow(new RuntimeException("Connection refused"));

        // Act & Assert
        assertThatThrownBy(() -> tracedApi.generate(MODEL_NAME, "Test"))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Connection refused");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== chat Tests ==========

    @Test
    void shouldCreateSpanForChat() throws Exception {
        // Arrange
        OllamaChatMessage userMsg = mock(OllamaChatMessage.class);
        when(userMsg.getRole()).thenReturn(OllamaChatMessageRole.USER);
        when(userMsg.getContent()).thenReturn("Hello!");
        List<OllamaChatMessage> messages = List.of(userMsg);

        when(mockApi.chat(MODEL_NAME, messages)).thenReturn(mockChatResult);
        when(mockChatResult.getResponse()).thenReturn("Hi there!");
        when(mockChatResult.getResponseTime()).thenReturn(0L);

        // Act
        OllamaChatResult result = tracedApi.chat(MODEL_NAME, messages);

        // Assert
        assertThat(result).isSameAs(mockChatResult);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Ollama Chat");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("ollama");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldCaptureInputMessagesForChat() throws Exception {
        // Arrange
        OllamaChatMessage userMsg = mock(OllamaChatMessage.class);
        when(userMsg.getRole()).thenReturn(OllamaChatMessageRole.USER);
        when(userMsg.getContent()).thenReturn("What is AI?");
        List<OllamaChatMessage> messages = List.of(userMsg);

        when(mockApi.chat(MODEL_NAME, messages)).thenReturn(mockChatResult);
        when(mockChatResult.getResponse()).thenReturn("AI is...");
        when(mockChatResult.getResponseTime()).thenReturn(0L);

        // Act
        tracedApi.chat(MODEL_NAME, messages);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("What is AI?");
    }

    @Test
    void shouldRecordErrorOnChatFailure() throws Exception {
        // Arrange
        OllamaChatMessage userMsg = mock(OllamaChatMessage.class);
        when(userMsg.getRole()).thenReturn(OllamaChatMessageRole.USER);
        when(userMsg.getContent()).thenReturn("Hello!");
        List<OllamaChatMessage> messages = List.of(userMsg);

        when(mockApi.chat(MODEL_NAME, messages))
            .thenThrow(new RuntimeException("Model not found"));

        // Act & Assert
        assertThatThrownBy(() -> tracedApi.chat(MODEL_NAME, messages))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Model not found");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== embed Tests ==========

    @Test
    void shouldCreateSpanForEmbed() throws Exception {
        // Arrange
        List<Double> embedding = List.of(0.1, 0.2, 0.3, 0.4, 0.5);
        when(mockApi.generateEmbeddings(MODEL_NAME, "Hello, world!")).thenReturn(embedding);

        // Act
        List<Double> result = tracedApi.embed(MODEL_NAME, "Hello, world!");

        // Assert
        assertThat(result).isSameAs(embedding);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Ollama Embed");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("ollama");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetEmbeddingDimensionsForEmbed() throws Exception {
        // Arrange
        List<Double> embedding = List.of(0.1, 0.2, 0.3);
        when(mockApi.generateEmbeddings(MODEL_NAME, "Test")).thenReturn(embedding);

        // Act
        tracedApi.embed(MODEL_NAME, "Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)
        )).isEqualTo(3L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT)
        )).isEqualTo(1L);
    }

    @Test
    void shouldSetEmbeddingModelNameForEmbed() throws Exception {
        // Arrange
        List<Double> embedding = List.of(0.1, 0.2);
        when(mockApi.generateEmbeddings(MODEL_NAME, "Test")).thenReturn(embedding);

        // Act
        tracedApi.embed(MODEL_NAME, "Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.EMBEDDING_MODEL_NAME)
        )).isEqualTo(MODEL_NAME);
    }

    @Test
    void shouldRecordErrorOnEmbedFailure() throws Exception {
        // Arrange
        when(mockApi.generateEmbeddings(MODEL_NAME, "Test"))
            .thenThrow(new RuntimeException("Embedding failed"));

        // Act & Assert
        assertThatThrownBy(() -> tracedApi.embed(MODEL_NAME, "Test"))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Embedding failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== unwrap Tests ==========

    @Test
    void shouldReturnUnwrappedApi() {
        assertThat(tracedApi.unwrap()).isSameAs(mockApi);
    }

    @Test
    void shouldReturnSameApiOnMultipleUnwrapCalls() {
        assertThat(tracedApi.unwrap()).isSameAs(tracedApi.unwrap());
    }
}
