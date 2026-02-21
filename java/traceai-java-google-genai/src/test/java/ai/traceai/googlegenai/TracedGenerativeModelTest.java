package ai.traceai.googlegenai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.genai.Client;
import com.google.genai.Models;
import com.google.genai.types.GenerateContentResponse;
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

import java.lang.reflect.Field;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedGenerativeModel (Google GenAI).
 */
@ExtendWith(MockitoExtension.class)
class TracedGenerativeModelTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private Client mockClient;

    @Mock
    private Models mockModels;

    @Mock
    private GenerateContentResponse mockResponse;

    private FITracer tracer;
    private TracedGenerativeModel tracedModel;

    private static final String MODEL_NAME = "gemini-1.5-flash";

    @BeforeEach
    void setup() throws Exception {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());

        // Client.models is a public field, set it via reflection since Client is mocked
        Field modelsField = Client.class.getDeclaredField("models");
        modelsField.setAccessible(true);
        modelsField.set(mockClient, mockModels);

        tracedModel = new TracedGenerativeModel(mockClient, tracer, MODEL_NAME);
    }

    // ========== generateContent Tests ==========

    @Test
    void shouldCreateSpanForGenerateContent() throws Exception {
        // Arrange
        when(mockModels.generateContent(eq(MODEL_NAME), eq("Hello!"), isNull()))
            .thenReturn(mockResponse);
        when(mockResponse.text()).thenReturn("Hi there!");

        // Act
        GenerateContentResponse result = tracedModel.generateContent("Hello!");

        // Assert
        assertThat(result).isSameAs(mockResponse);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Google GenAI Generate Content");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("google");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetModelAttributesForGenerateContent() throws Exception {
        // Arrange
        when(mockModels.generateContent(eq(MODEL_NAME), eq("Test"), isNull()))
            .thenReturn(mockResponse);
        when(mockResponse.text()).thenReturn("Response");

        // Act
        tracedModel.generateContent("Test");

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
    void shouldCaptureOutputValueForGenerateContent() throws Exception {
        // Arrange
        String expectedOutput = "This is the generated content";
        when(mockModels.generateContent(eq(MODEL_NAME), eq("Hello!"), isNull()))
            .thenReturn(mockResponse);
        when(mockResponse.text()).thenReturn(expectedOutput);

        // Act
        tracedModel.generateContent("Hello!");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);
    }

    @Test
    void shouldCaptureInputMessagesForGenerateContent() throws Exception {
        // Arrange
        when(mockModels.generateContent(eq(MODEL_NAME), eq("Hello!"), isNull()))
            .thenReturn(mockResponse);
        when(mockResponse.text()).thenReturn("Response");

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
        assertThat(inputMessages).contains("Hello!");
    }

    @Test
    void shouldRecordErrorOnGenerateContentFailure() throws Exception {
        // Arrange
        when(mockModels.generateContent(eq(MODEL_NAME), eq("Test"), isNull()))
            .thenThrow(new RuntimeException("API quota exceeded"));

        // Act & Assert
        // TracedGenerativeModel wraps in RuntimeException("Google GenAI generate content failed", e)
        assertThatThrownBy(() -> tracedModel.generateContent("Test"))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Google GenAI generate content failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldSetSystemProviderAttribute() throws Exception {
        // Arrange
        when(mockModels.generateContent(eq(MODEL_NAME), eq("Test"), isNull()))
            .thenReturn(mockResponse);
        when(mockResponse.text()).thenReturn("Response");

        // Act
        tracedModel.generateContent("Test");

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // LLM_SYSTEM and LLM_PROVIDER both map to "gen_ai.provider.name",
        // last setAttribute wins (LLM_PROVIDER="google")
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("google");
    }

    // ========== unwrap Tests ==========

    @Test
    void shouldReturnUnwrappedClient() {
        assertThat(tracedModel.unwrap()).isSameAs(mockClient);
    }

    @Test
    void shouldReturnSameClientOnMultipleUnwrapCalls() {
        assertThat(tracedModel.unwrap()).isSameAs(tracedModel.unwrap());
    }

    // ========== getModelName Tests ==========

    @Test
    void shouldReturnModelName() {
        assertThat(tracedModel.getModelName()).isEqualTo(MODEL_NAME);
    }
}
