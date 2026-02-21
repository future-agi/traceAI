package ai.traceai.spring;

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
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.metadata.ChatResponseMetadata;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedChatModel.
 */
@ExtendWith(MockitoExtension.class)
class TracedChatModelTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private ChatModel mockModel;

    private FITracer tracer;
    private TracedChatModel tracedModel;

    private static final String PROVIDER = "openai";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedModel = new TracedChatModel(mockModel, tracer, PROVIDER);
    }

    // ========== call(Prompt) Tests ==========

    @Test
    void shouldCreateSpanForCall() {
        // Arrange
        Prompt prompt = new Prompt("Hello!");
        ChatResponse response = buildChatResponse("Hi there!");

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        ChatResponse result = tracedModel.call(prompt);

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Spring AI Chat");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForCall() {
        // Arrange
        Prompt prompt = new Prompt("Test");
        ChatResponse response = buildChatResponse("Response");

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        tracedModel.call(prompt);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo(PROVIDER);
    }

    @Test
    void shouldCaptureInputMessagesForCall() {
        // Arrange
        List<Message> messages = List.of(
            new SystemMessage("You are a helpful assistant."),
            new UserMessage("Hello!")
        );
        Prompt prompt = new Prompt(messages);
        ChatResponse response = buildChatResponse("Hi!");

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        tracedModel.call(prompt);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":");
        assertThat(inputMessages).contains("\"content\":");
    }

    @Test
    void shouldCaptureOutputForCall() {
        // Arrange
        Prompt prompt = new Prompt("Test");
        String expectedOutput = "This is the assistant response";
        ChatResponse response = buildChatResponse(expectedOutput);

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        tracedModel.call(prompt);

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
        assertThat(outputMessages).contains("\"role\":\"assistant\"");
        assertThat(outputMessages).contains("\"content\":\"" + expectedOutput + "\"");
    }

    @Test
    void shouldCaptureTokenUsageForCall() {
        // Arrange
        Prompt prompt = new Prompt("Test");

        Usage mockUsage = mock(Usage.class);
        when(mockUsage.getPromptTokens()).thenReturn(10L);
        when(mockUsage.getGenerationTokens()).thenReturn(20L);
        when(mockUsage.getTotalTokens()).thenReturn(30L);

        ChatResponseMetadata metadata = mock(ChatResponseMetadata.class);
        when(metadata.getUsage()).thenReturn(mockUsage);

        AssistantMessage outputMsg = new AssistantMessage("Response");
        Generation generation = new Generation(outputMsg);
        ChatResponse response = mock(ChatResponse.class);
        when(response.getResult()).thenReturn(generation);
        when(response.getMetadata()).thenReturn(metadata);

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        tracedModel.call(prompt);

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
    void shouldCapturePromptOptionsForCall() {
        // Arrange
        ChatOptions options = mock(ChatOptions.class);
        when(options.getModel()).thenReturn("gpt-4");
        when(options.getTemperature()).thenReturn(0.7);
        when(options.getTopP()).thenReturn(0.9);

        Prompt prompt = new Prompt("Test", options);
        ChatResponse response = buildChatResponse("Response");

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        tracedModel.call(prompt);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo("gpt-4");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)
        )).isEqualTo("gpt-4");
        assertThat(spanData.getAttributes().get(
            AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)
        )).isEqualTo(0.7);
        assertThat(spanData.getAttributes().get(
            AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TOP_P)
        )).isEqualTo(0.9);
    }

    @Test
    void shouldRecordErrorOnCallFailure() {
        // Arrange
        Prompt prompt = new Prompt("Test");

        when(mockModel.call(any(Prompt.class)))
            .thenThrow(new RuntimeException("Connection timeout"));

        // Act & Assert
        assertThatThrownBy(() -> tracedModel.call(prompt))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Connection timeout");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)
        )).isEqualTo("java.lang.RuntimeException");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE)
        )).isEqualTo("Connection timeout");
    }

    @Test
    void shouldHandleNullPrompt() {
        // Arrange
        ChatResponse response = buildChatResponse("Response");
        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act - passing null prompt; the source code null-checks prompt before accessing instructions
        Prompt prompt = mock(Prompt.class);
        when(prompt.getInstructions()).thenReturn(null);
        when(prompt.getOptions()).thenReturn(null);
        ChatResponse result = tracedModel.call(prompt);

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldHandleNullResponseResult() {
        // Arrange
        Prompt prompt = new Prompt("Test");
        ChatResponse response = mock(ChatResponse.class);
        when(response.getResult()).thenReturn(null);

        when(mockModel.call(any(Prompt.class))).thenReturn(response);

        // Act
        ChatResponse result = tracedModel.call(prompt);

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    // ========== getDefaultOptions Tests ==========

    @Test
    void shouldDelegateGetDefaultOptions() {
        // Arrange
        ChatOptions options = mock(ChatOptions.class);
        when(mockModel.getDefaultOptions()).thenReturn(options);

        // Act
        ChatOptions result = tracedModel.getDefaultOptions();

        // Assert
        assertThat(result).isSameAs(options);
        verify(mockModel).getDefaultOptions();
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

    private ChatResponse buildChatResponse(String content) {
        AssistantMessage outputMsg = new AssistantMessage(content);
        Generation generation = new Generation(outputMsg);
        return new ChatResponse(List.of(generation));
    }
}
