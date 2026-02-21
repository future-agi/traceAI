package ai.traceai.vertexai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.cloud.vertexai.api.*;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
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

import java.io.IOException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedGenerativeModel.
 */
@ExtendWith(MockitoExtension.class)
class TracedGenerativeModelTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private GenerativeModel mockModel;

    private FITracer tracer;
    private TracedGenerativeModel tracedModel;

    private static final String MODEL_NAME = "gemini-1.5-pro";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        when(mockModel.getModelName()).thenReturn(MODEL_NAME);
        tracedModel = new TracedGenerativeModel(mockModel, tracer);
    }

    // ========== generateContent(String) Tests ==========

    @Test
    void shouldCreateSpanForGenerateContent() throws IOException {
        // Arrange
        GenerateContentResponse response = buildResponse("Hi there!", "STOP");

        when(mockModel.generateContent(anyString())).thenReturn(response);

        // Act
        GenerateContentResponse result = tracedModel.generateContent("Hello!");

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Vertex AI Generate Content");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForGenerateContent() throws IOException {
        // Arrange
        GenerateContentResponse response = buildResponse("Response", "STOP");

        when(mockModel.generateContent(anyString())).thenReturn(response);

        // Act
        tracedModel.generateContent("Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("google");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo(MODEL_NAME);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)
        )).isEqualTo(MODEL_NAME);
    }

    @Test
    void shouldCaptureInputMessagesForGenerateContent() throws IOException {
        // Arrange
        GenerateContentResponse response = buildResponse("Response", "STOP");

        when(mockModel.generateContent(anyString())).thenReturn(response);

        // Act
        tracedModel.generateContent("Hello!");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("\"content\":\"Hello!\"");
    }

    @Test
    void shouldCaptureOutputForGenerateContent() throws IOException {
        // Arrange
        String expectedOutput = "This is the model response";
        GenerateContentResponse response = buildResponse(expectedOutput, "STOP");

        when(mockModel.generateContent(anyString())).thenReturn(response);

        // Act
        tracedModel.generateContent("Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);

        String outputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES)
        );
        assertThat(outputMessages).isNotNull();
        assertThat(outputMessages).contains("\"role\":\"model\"");
        assertThat(outputMessages).contains("\"content\":\"" + expectedOutput + "\"");
    }

    @Test
    void shouldCaptureTokenUsageForGenerateContent() throws IOException {
        // Arrange
        GenerateContentResponse.UsageMetadata usageMetadata =
            GenerateContentResponse.UsageMetadata.newBuilder()
                .setPromptTokenCount(10)
                .setCandidatesTokenCount(20)
                .setTotalTokenCount(30)
                .build();

        GenerateContentResponse response = GenerateContentResponse.newBuilder()
            .addCandidates(Candidate.newBuilder()
                .setContent(Content.newBuilder()
                    .setRole("model")
                    .addParts(Part.newBuilder().setText("Response").build())
                    .build())
                .setFinishReason(Candidate.FinishReason.STOP)
                .build())
            .setUsageMetadata(usageMetadata)
            .build();

        when(mockModel.generateContent(anyString())).thenReturn(response);

        // Act
        tracedModel.generateContent("Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)
        )).isEqualTo(10L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)
        )).isEqualTo(20L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)
        )).isEqualTo(30L);
    }

    @Test
    void shouldRecordErrorOnGenerateContentFailure() throws IOException {
        // Arrange
        when(mockModel.generateContent(anyString()))
            .thenThrow(new IOException("Service unavailable"));

        // Act & Assert
        assertThatThrownBy(() -> tracedModel.generateContent("Test"))
            .isInstanceOf(IOException.class)
            .hasMessageContaining("Service unavailable");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)
        )).isEqualTo("java.io.IOException");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE)
        )).isEqualTo("Service unavailable");
    }

    // ========== generateContent(List<Content>) Tests ==========

    @Test
    void shouldCreateSpanForGenerateContentWithContents() throws IOException {
        // Arrange
        Content content = Content.newBuilder()
            .setRole("user")
            .addParts(Part.newBuilder().setText("Hello!").build())
            .build();

        GenerateContentResponse response = buildResponse("Hi!", "STOP");

        when(mockModel.generateContent(any(List.class))).thenReturn(response);

        // Act
        GenerateContentResponse result = tracedModel.generateContent(List.of(content));

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Vertex AI Generate Content");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldRecordErrorOnGenerateContentWithContentsFailure() throws IOException {
        // Arrange
        Content content = Content.newBuilder()
            .setRole("user")
            .addParts(Part.newBuilder().setText("Test").build())
            .build();

        when(mockModel.generateContent(any(List.class)))
            .thenThrow(new IOException("Quota exceeded"));

        // Act & Assert
        assertThatThrownBy(() -> tracedModel.generateContent(List.of(content)))
            .isInstanceOf(IOException.class)
            .hasMessageContaining("Quota exceeded");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== countTokens Tests ==========

    @Test
    void shouldCreateSpanForCountTokens() throws IOException {
        // Arrange
        CountTokensResponse response = CountTokensResponse.newBuilder()
            .setTotalTokens(42)
            .build();

        when(mockModel.countTokens(anyString())).thenReturn(response);

        // Act
        CountTokensResponse result = tracedModel.countTokens("Test text");

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Vertex AI Count Tokens");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)
        )).isEqualTo(42L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)
        )).isEqualTo(42L);
    }

    @Test
    void shouldRecordErrorOnCountTokensFailure() throws IOException {
        // Arrange
        when(mockModel.countTokens(anyString()))
            .thenThrow(new IOException("Connection refused"));

        // Act & Assert
        assertThatThrownBy(() -> tracedModel.countTokens("Test"))
            .isInstanceOf(IOException.class)
            .hasMessageContaining("Connection refused");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== unwrap Tests ==========

    @Test
    void shouldReturnUnwrappedModel() {
        assertThat(tracedModel.unwrap()).isSameAs(mockModel);
    }

    @Test
    void shouldReturnSameModelOnMultipleUnwrapCalls() {
        assertThat(tracedModel.unwrap()).isSameAs(tracedModel.unwrap());
    }

    // ========== Helper Methods ==========

    private GenerateContentResponse buildResponse(String text, String finishReason) {
        return GenerateContentResponse.newBuilder()
            .addCandidates(Candidate.newBuilder()
                .setContent(Content.newBuilder()
                    .setRole("model")
                    .addParts(Part.newBuilder().setText(text).build())
                    .build())
                .setFinishReason(Candidate.FinishReason.valueOf(finishReason))
                .build())
            .build();
    }
}
