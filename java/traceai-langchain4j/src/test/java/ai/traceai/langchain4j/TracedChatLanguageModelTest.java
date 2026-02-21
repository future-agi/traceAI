package ai.traceai.langchain4j;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.FinishReason;
import dev.langchain4j.model.output.Response;
import dev.langchain4j.model.output.TokenUsage;
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
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedChatLanguageModel.
 */
@ExtendWith(MockitoExtension.class)
class TracedChatLanguageModelTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private ChatLanguageModel mockModel;

    private FITracer tracer;
    private TracedChatLanguageModel tracedModel;

    private static final String PROVIDER = "openai";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedModel = new TracedChatLanguageModel(mockModel, tracer, PROVIDER);
    }

    // ========== generate(List<ChatMessage>) Tests ==========

    @Test
    void shouldCreateSpanForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("Hello!"));
        AiMessage aiMessage = AiMessage.from("Hi there!");
        Response<AiMessage> response = new Response<>(aiMessage);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        Response<AiMessage> result = tracedModel.generate(messages);

        // Assert
        assertThat(result).isSameAs(response);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("LangChain4j Chat");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("Test"));
        AiMessage aiMessage = AiMessage.from("Response");
        Response<AiMessage> response = new Response<>(aiMessage);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        tracedModel.generate(messages);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo(PROVIDER);
    }

    @Test
    void shouldCaptureInputMessagesForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(
            SystemMessage.from("You are a helpful assistant."),
            UserMessage.from("Hello!")
        );
        AiMessage aiMessage = AiMessage.from("Hi!");
        Response<AiMessage> response = new Response<>(aiMessage);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        tracedModel.generate(messages);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"system\"");
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("\"content\":\"Hello!\"");
    }

    @Test
    void shouldCaptureOutputForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("Test"));
        String expectedOutput = "This is the assistant response";
        AiMessage aiMessage = AiMessage.from(expectedOutput);
        Response<AiMessage> response = new Response<>(aiMessage);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        tracedModel.generate(messages);

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
    void shouldCaptureTokenUsageForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("Test"));
        AiMessage aiMessage = AiMessage.from("Response");
        TokenUsage tokenUsage = new TokenUsage(10, 20, 30);
        Response<AiMessage> response = new Response<>(aiMessage, tokenUsage, FinishReason.STOP);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        tracedModel.generate(messages);

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
    void shouldCaptureFinishReasonForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("Test"));
        AiMessage aiMessage = AiMessage.from("Response");
        Response<AiMessage> response = new Response<>(aiMessage, null, FinishReason.STOP);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        tracedModel.generate(messages);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON)
        )).isEqualTo("STOP");
    }

    @Test
    void shouldCaptureToolCallsForGenerate() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("What's the weather?"));
        ToolExecutionRequest toolRequest = ToolExecutionRequest.builder()
            .id("call_123")
            .name("get_weather")
            .arguments("{\"location\": \"New York\"}")
            .build();
        AiMessage aiMessage = AiMessage.from(toolRequest);
        Response<AiMessage> response = new Response<>(aiMessage);

        when(mockModel.generate(any(List.class))).thenReturn(response);

        // Act
        tracedModel.generate(messages);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.tool_calls.0.id")
        )).isEqualTo("call_123");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.tool_calls.0.name")
        )).isEqualTo("get_weather");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.tool_calls.0.arguments")
        )).isEqualTo("{\"location\": \"New York\"}");
    }

    @Test
    void shouldRecordErrorOnGenerateFailure() {
        // Arrange
        List<ChatMessage> messages = List.of(UserMessage.from("Test"));

        when(mockModel.generate(any(List.class)))
            .thenThrow(new RuntimeException("API rate limit exceeded"));

        // Act & Assert
        assertThatThrownBy(() -> tracedModel.generate(messages))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("API rate limit exceeded");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)
        )).isEqualTo("java.lang.RuntimeException");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE)
        )).isEqualTo("API rate limit exceeded");
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
}
