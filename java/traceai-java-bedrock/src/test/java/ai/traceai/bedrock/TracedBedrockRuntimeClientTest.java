package ai.traceai.bedrock;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
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
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedBedrockRuntimeClient.
 */
@ExtendWith(MockitoExtension.class)
class TracedBedrockRuntimeClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private BedrockRuntimeClient mockClient;

    private FITracer tracer;
    private TracedBedrockRuntimeClient tracedClient;

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedClient = new TracedBedrockRuntimeClient(mockClient, tracer);
    }

    // ========== invokeModel Tests ==========

    @Test
    void shouldCreateSpanForInvokeModel() {
        // Arrange
        InvokeModelRequest request = InvokeModelRequest.builder()
            .modelId("anthropic.claude-v2")
            .body(SdkBytes.fromUtf8String("{\"prompt\": \"Hello!\", \"max_tokens_to_sample\": 200}"))
            .build();

        InvokeModelResponse response = InvokeModelResponse.builder()
            .body(SdkBytes.fromUtf8String("{\"completion\": \"Hi there!\"}"))
            .build();

        when(mockClient.invokeModel(any(InvokeModelRequest.class))).thenReturn(response);

        // Act
        InvokeModelResponse result = tracedClient.invokeModel(request);

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Bedrock Invoke Model");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForInvokeModel() {
        // Arrange
        InvokeModelRequest request = InvokeModelRequest.builder()
            .modelId("anthropic.claude-v2")
            .body(SdkBytes.fromUtf8String("{\"prompt\": \"Test\"}"))
            .build();

        InvokeModelResponse response = InvokeModelResponse.builder()
            .body(SdkBytes.fromUtf8String("{\"completion\": \"Response\"}"))
            .build();

        when(mockClient.invokeModel(any(InvokeModelRequest.class))).thenReturn(response);

        // Act
        tracedClient.invokeModel(request);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("anthropic");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo("anthropic.claude-v2");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)
        )).isEqualTo("anthropic.claude-v2");
    }

    @Test
    void shouldRecordErrorOnInvokeModelFailure() {
        // Arrange
        InvokeModelRequest request = InvokeModelRequest.builder()
            .modelId("anthropic.claude-v2")
            .body(SdkBytes.fromUtf8String("{\"prompt\": \"Test\"}"))
            .build();

        when(mockClient.invokeModel(any(InvokeModelRequest.class)))
            .thenThrow(new RuntimeException("Throttling exception"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.invokeModel(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Throttling exception");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)
        )).isEqualTo("java.lang.RuntimeException");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE)
        )).isEqualTo("Throttling exception");
    }

    @Test
    void shouldExtractProviderFromModelId() {
        // Arrange - use Amazon model to verify provider extraction
        InvokeModelRequest request = InvokeModelRequest.builder()
            .modelId("amazon.titan-text-express-v1")
            .body(SdkBytes.fromUtf8String("{\"inputText\": \"Hello\"}"))
            .build();

        InvokeModelResponse response = InvokeModelResponse.builder()
            .body(SdkBytes.fromUtf8String("{\"results\": [{\"outputText\": \"Hi\"}]}"))
            .build();

        when(mockClient.invokeModel(any(InvokeModelRequest.class))).thenReturn(response);

        // Act
        tracedClient.invokeModel(request);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("amazon");
    }

    // ========== converse Tests ==========

    @Test
    void shouldCreateSpanForConverse() {
        // Arrange
        ContentBlock contentBlock = ContentBlock.builder()
            .text("Hello!")
            .build();
        Message inputMessage = Message.builder()
            .role(ConversationRole.USER)
            .content(contentBlock)
            .build();
        ConverseRequest request = ConverseRequest.builder()
            .modelId("anthropic.claude-3-sonnet-20240229-v1:0")
            .messages(inputMessage)
            .build();

        ContentBlock outputContent = ContentBlock.builder()
            .text("Hi there!")
            .build();
        Message outputMessage = Message.builder()
            .role(ConversationRole.ASSISTANT)
            .content(outputContent)
            .build();
        ConverseOutput output = ConverseOutput.builder()
            .message(outputMessage)
            .build();
        TokenUsage usage = TokenUsage.builder()
            .inputTokens(10)
            .outputTokens(20)
            .totalTokens(30)
            .build();
        ConverseResponse response = ConverseResponse.builder()
            .output(output)
            .usage(usage)
            .stopReason(StopReason.END_TURN)
            .build();

        when(mockClient.converse(any(ConverseRequest.class))).thenReturn(response);

        // Act
        ConverseResponse result = tracedClient.converse(request);

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Bedrock Converse");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForConverse() {
        // Arrange
        ContentBlock contentBlock = ContentBlock.builder()
            .text("Test")
            .build();
        Message inputMessage = Message.builder()
            .role(ConversationRole.USER)
            .content(contentBlock)
            .build();
        ConverseRequest request = ConverseRequest.builder()
            .modelId("anthropic.claude-3-sonnet-20240229-v1:0")
            .messages(inputMessage)
            .build();

        ConverseResponse response = ConverseResponse.builder().build();
        when(mockClient.converse(any(ConverseRequest.class))).thenReturn(response);

        // Act
        tracedClient.converse(request);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("anthropic");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo("anthropic.claude-3-sonnet-20240229-v1:0");
    }

    @Test
    void shouldRecordErrorOnConverseFailure() {
        // Arrange
        ContentBlock contentBlock = ContentBlock.builder()
            .text("Test")
            .build();
        Message inputMessage = Message.builder()
            .role(ConversationRole.USER)
            .content(contentBlock)
            .build();
        ConverseRequest request = ConverseRequest.builder()
            .modelId("anthropic.claude-3-sonnet-20240229-v1:0")
            .messages(inputMessage)
            .build();

        when(mockClient.converse(any(ConverseRequest.class)))
            .thenThrow(new RuntimeException("Model not available"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.converse(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Model not available");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)
        )).isEqualTo("java.lang.RuntimeException");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE)
        )).isEqualTo("Model not available");
    }

    @Test
    void shouldCaptureTokenUsageForConverse() {
        // Arrange
        ContentBlock contentBlock = ContentBlock.builder()
            .text("Test")
            .build();
        Message inputMessage = Message.builder()
            .role(ConversationRole.USER)
            .content(contentBlock)
            .build();
        ConverseRequest request = ConverseRequest.builder()
            .modelId("anthropic.claude-3-sonnet-20240229-v1:0")
            .messages(inputMessage)
            .build();

        TokenUsage usage = TokenUsage.builder()
            .inputTokens(15)
            .outputTokens(25)
            .totalTokens(40)
            .build();
        ConverseResponse response = ConverseResponse.builder()
            .usage(usage)
            .build();

        when(mockClient.converse(any(ConverseRequest.class))).thenReturn(response);

        // Act
        tracedClient.converse(request);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)
        )).isEqualTo(15L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)
        )).isEqualTo(25L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)
        )).isEqualTo(40L);
    }

    // ========== unwrap Tests ==========

    @Test
    void shouldReturnUnwrappedClient() {
        assertThat(tracedClient.unwrap()).isSameAs(mockClient);
    }

    @Test
    void shouldReturnSameClientOnMultipleUnwrapCalls() {
        assertThat(tracedClient.unwrap()).isSameAs(tracedClient.unwrap());
    }
}
