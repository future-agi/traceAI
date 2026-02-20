package ai.traceai.azure.openai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.models.*;
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

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedAzureOpenAIClient.
 */
@ExtendWith(MockitoExtension.class)
class TracedAzureOpenAIClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private OpenAIClient mockOpenAIClient;

    @Mock
    private ChatCompletions mockChatCompletions;

    @Mock
    private Embeddings mockEmbeddings;

    @Mock
    private Completions mockCompletions;

    @Mock
    private CompletionsUsage mockCompletionsUsage;

    @Mock
    private EmbeddingsUsage mockEmbeddingsUsage;

    private FITracer tracer;
    private TracedAzureOpenAIClient tracedClient;

    private static final String MODEL_NAME = "gpt-4";
    private static final String EMBEDDING_MODEL = "text-embedding-ada-002";
    private static final String RESPONSE_ID = "chatcmpl-12345";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedClient = new TracedAzureOpenAIClient(mockOpenAIClient, tracer);
    }

    // ========== getChatCompletions Tests ==========

    @Test
    void shouldCreateSpanForChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Hello, how are you?");
        setupMockChatCompletions("I'm doing well, thank you!");

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        ChatCompletions result = tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        assertThat(result).isSameAs(mockChatCompletions);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure OpenAI Chat Completion");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");
        setupMockChatCompletions("Response message");

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // last setAttribute call wins (LLM_PROVIDER="azure")
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("azure");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo(MODEL_NAME);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)
        )).isEqualTo(MODEL_NAME);
    }

    @Test
    void shouldSetRequestParametersForChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");
        options.setTemperature(0.7);
        options.setTopP(0.9);
        options.setMaxTokens(100);
        setupMockChatCompletions("Response message");

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)
        )).isEqualTo(0.7);
        assertThat(spanData.getAttributes().get(
            AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TOP_P)
        )).isEqualTo(0.9);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_REQUEST_MAX_TOKENS)
        )).isEqualTo(100L);
    }

    @Test
    void shouldSetResponseAttributesForChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");
        setupMockChatCompletions("Response message");
        when(mockChatCompletions.getModel()).thenReturn("gpt-4-0613");
        when(mockChatCompletions.getId()).thenReturn(RESPONSE_ID);

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_MODEL)
        )).isEqualTo("gpt-4-0613");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_ID)
        )).isEqualTo(RESPONSE_ID);
    }

    @Test
    void shouldSetTokenCountsForChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");
        setupMockChatCompletions("Response message");
        setupMockCompletionsUsage(10, 20, 30);
        when(mockChatCompletions.getUsage()).thenReturn(mockCompletionsUsage);

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

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
    void shouldCaptureInputMessagesForChatCompletions() {
        // Arrange
        ChatRequestUserMessage userMessage = new ChatRequestUserMessage("Hello!");
        ChatCompletionsOptions options = new ChatCompletionsOptions(List.of(userMessage));
        setupMockChatCompletions("Hi there!");

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // Messages are now stored as JSON blob under gen_ai.input.messages
        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("\"content\":\"Hello!\"");
    }

    @Test
    void shouldCaptureSystemMessageRole() {
        // Arrange
        ChatRequestSystemMessage systemMessage = new ChatRequestSystemMessage("You are a helpful assistant.");
        ChatRequestUserMessage userMessage = new ChatRequestUserMessage("Hello!");
        ChatCompletionsOptions options = new ChatCompletionsOptions(List.of(systemMessage, userMessage));
        setupMockChatCompletions("Hi there!");

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // Messages are now stored as JSON blob under gen_ai.input.messages
        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"system\"");
        assertThat(inputMessages).contains("\"role\":\"user\"");
    }

    @Test
    void shouldCaptureOutputMessagesForChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");

        ChatChoice mockChoice = mock(ChatChoice.class);
        ChatResponseMessage mockMessage = mock(ChatResponseMessage.class);
        when(mockMessage.getRole()).thenReturn(ChatRole.ASSISTANT);
        when(mockMessage.getContent()).thenReturn("This is the response");
        when(mockChoice.getMessage()).thenReturn(mockMessage);
        when(mockChoice.getFinishReason()).thenReturn(CompletionsFinishReason.STOPPED);
        when(mockChatCompletions.getChoices()).thenReturn(List.of(mockChoice));

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // Output messages are now stored as JSON blob under gen_ai.output.messages
        String outputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES)
        );
        assertThat(outputMessages).isNotNull();
        assertThat(outputMessages).contains("\"role\":");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON)
        )).isEqualTo("stop");
    }

    @Test
    void shouldSetOutputValueFromFirstChoice() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");
        String expectedOutput = "This is the assistant response";
        setupMockChatCompletions(expectedOutput);

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);
    }

    @Test
    void shouldRecordErrorOnChatCompletionsFailure() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenThrow(new RuntimeException("API connection error"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.getChatCompletions(MODEL_NAME, options))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("API connection error");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)
        )).isEqualTo("java.lang.RuntimeException");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE)
        )).isEqualTo("API connection error");
    }

    @Test
    void shouldHandleNullMessagesInChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = new ChatCompletionsOptions(null);
        when(mockChatCompletions.getChoices()).thenReturn(Collections.emptyList());

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        ChatCompletions result = tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        assertThat(result).isSameAs(mockChatCompletions);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldHandleEmptyChoicesInChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("Test message");
        when(mockChatCompletions.getChoices()).thenReturn(Collections.emptyList());

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        ChatCompletions result = tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        assertThat(result).isSameAs(mockChatCompletions);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldCaptureToolCallsFromChatCompletions() {
        // Arrange
        ChatCompletionsOptions options = createChatCompletionsOptions("What's the weather?");

        ChatChoice mockChoice = mock(ChatChoice.class);
        ChatResponseMessage mockMessage = mock(ChatResponseMessage.class);
        ChatCompletionsFunctionToolCall mockToolCall = mock(ChatCompletionsFunctionToolCall.class);
        FunctionCall mockFunction = mock(FunctionCall.class);

        when(mockFunction.getName()).thenReturn("get_weather");
        when(mockFunction.getArguments()).thenReturn("{\"location\": \"New York\"}");
        when(mockToolCall.getId()).thenReturn("call_12345");
        when(mockToolCall.getFunction()).thenReturn(mockFunction);
        when(mockMessage.getRole()).thenReturn(ChatRole.ASSISTANT);
        when(mockMessage.getContent()).thenReturn(null);
        when(mockMessage.getToolCalls()).thenReturn(List.of(mockToolCall));
        when(mockChoice.getMessage()).thenReturn(mockMessage);
        when(mockChatCompletions.getChoices()).thenReturn(List.of(mockChoice));

        when(mockOpenAIClient.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(mockChatCompletions);

        // Act
        tracedClient.getChatCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.output_messages.0.tool_calls.0.id")
        )).isEqualTo("call_12345");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.output_messages.0.tool_calls.0.function.name")
        )).isEqualTo("get_weather");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.output_messages.0.tool_calls.0.function.arguments")
        )).isEqualTo("{\"location\": \"New York\"}");
    }

    // ========== getEmbeddings Tests ==========

    @Test
    void shouldCreateSpanForEmbeddings() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Hello, world!"));
        setupMockEmbeddings(1536);

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenReturn(mockEmbeddings);

        // Act
        Embeddings result = tracedClient.getEmbeddings(EMBEDDING_MODEL, options);

        // Assert
        assertThat(result).isSameAs(mockEmbeddings);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure OpenAI Embedding");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForEmbeddings() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Test text"));
        setupMockEmbeddings(1536);

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenReturn(mockEmbeddings);

        // Act
        tracedClient.getEmbeddings(EMBEDDING_MODEL, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // last setAttribute call wins (LLM_PROVIDER="azure")
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("azure");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.EMBEDDING_MODEL_NAME)
        )).isEqualTo(EMBEDDING_MODEL);
    }

    @Test
    void shouldSetEmbeddingMetadata() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Text 1", "Text 2"));
        setupMockEmbeddings(1536, 2);

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenReturn(mockEmbeddings);

        // Act
        tracedClient.getEmbeddings(EMBEDDING_MODEL, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT)
        )).isEqualTo(2L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)
        )).isEqualTo(1536L);
    }

    @Test
    void shouldSetTokenCountsForEmbeddings() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Test text"));
        setupMockEmbeddings(1536);
        when(mockEmbeddingsUsage.getPromptTokens()).thenReturn(5);
        when(mockEmbeddingsUsage.getTotalTokens()).thenReturn(5);
        when(mockEmbeddings.getUsage()).thenReturn(mockEmbeddingsUsage);

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenReturn(mockEmbeddings);

        // Act
        tracedClient.getEmbeddings(EMBEDDING_MODEL, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)
        )).isEqualTo(5L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)
        )).isEqualTo(5L);
    }

    @Test
    void shouldRecordErrorOnEmbeddingsFailure() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Test text"));

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenThrow(new RuntimeException("Embedding service unavailable"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.getEmbeddings(EMBEDDING_MODEL, options))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Embedding service unavailable");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldHandleEmptyEmbeddingsData() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Test text"));
        when(mockEmbeddings.getData()).thenReturn(Collections.emptyList());

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenReturn(mockEmbeddings);

        // Act
        Embeddings result = tracedClient.getEmbeddings(EMBEDDING_MODEL, options);

        // Assert
        assertThat(result).isSameAs(mockEmbeddings);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldHandleNullEmbeddingsData() {
        // Arrange
        EmbeddingsOptions options = new EmbeddingsOptions(List.of("Test text"));
        when(mockEmbeddings.getData()).thenReturn(null);

        when(mockOpenAIClient.getEmbeddings(anyString(), any(EmbeddingsOptions.class)))
            .thenReturn(mockEmbeddings);

        // Act
        Embeddings result = tracedClient.getEmbeddings(EMBEDDING_MODEL, options);

        // Assert
        assertThat(result).isSameAs(mockEmbeddings);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    // ========== getCompletions Tests ==========

    @Test
    void shouldCreateSpanForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Once upon a time"));
        setupMockCompletions("there was a princess");

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        Completions result = tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        assertThat(result).isSameAs(mockCompletions);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Azure OpenAI Completion");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetSystemAttributesForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        setupMockCompletions("Completion text");

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // last setAttribute call wins (LLM_PROVIDER="azure")
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("azure");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo(MODEL_NAME);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)
        )).isEqualTo(MODEL_NAME);
    }

    @Test
    void shouldSetRequestParametersForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        options.setTemperature(0.5);
        options.setTopP(0.8);
        options.setMaxTokens(50);
        setupMockCompletions("Completion text");

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)
        )).isEqualTo(0.5);
        assertThat(spanData.getAttributes().get(
            AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TOP_P)
        )).isEqualTo(0.8);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_REQUEST_MAX_TOKENS)
        )).isEqualTo(50L);
    }

    @Test
    void shouldCaptureInputPromptsForCompletions() {
        // Arrange
        List<String> prompts = List.of("Prompt 1", "Prompt 2");
        CompletionsOptions options = new CompletionsOptions(prompts);
        setupMockCompletions("Completion text");

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // Messages are now stored as JSON blob under gen_ai.input.messages
        String inputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)
        );
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("\"content\":\"Prompt 1\"");
        assertThat(inputMessages).contains("\"content\":\"Prompt 2\"");
    }

    @Test
    void shouldSetResponseAttributesForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        setupMockCompletions("Completion text");
        when(mockCompletions.getId()).thenReturn(RESPONSE_ID);

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // Note: getCompletions does not call result.getModel(), so LLM_RESPONSE_MODEL is not set
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_ID)
        )).isEqualTo(RESPONSE_ID);
    }

    @Test
    void shouldCaptureOutputChoicesForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        String completionText = "This is the completion output";
        setupMockCompletions(completionText);

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        // Output messages are now stored as JSON blob under gen_ai.output.messages
        String outputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES)
        );
        assertThat(outputMessages).isNotNull();
        assertThat(outputMessages).contains("\"role\":\"assistant\"");
        assertThat(outputMessages).contains("\"content\":\"" + completionText + "\"");
    }

    @Test
    void shouldSetTokenCountsForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        setupMockCompletions("Completion text");
        setupMockCompletionsUsage(5, 15, 20);
        when(mockCompletions.getUsage()).thenReturn(mockCompletionsUsage);

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)
        )).isEqualTo(5L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)
        )).isEqualTo(15L);
        assertThat(spanData.getAttributes().get(
            AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)
        )).isEqualTo(20L);
    }

    @Test
    void shouldSetOutputValueFromFirstCompletionChoice() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        String expectedOutput = "This is the completion output";
        setupMockCompletions(expectedOutput);

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);
    }

    @Test
    void shouldSetFinishReasonForCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));

        Choice mockChoice = mock(Choice.class);
        when(mockChoice.getText()).thenReturn("Completion text");
        when(mockChoice.getFinishReason()).thenReturn(CompletionsFinishReason.STOPPED);
        when(mockCompletions.getChoices()).thenReturn(List.of(mockChoice));

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON)
        )).isEqualTo("stop");
    }

    @Test
    void shouldRecordErrorOnCompletionsFailure() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenThrow(new RuntimeException("Rate limit exceeded"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.getCompletions(MODEL_NAME, options))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Rate limit exceeded");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldHandleNullPromptsInCompletions() {
        // Arrange
        CompletionsOptions options = mock(CompletionsOptions.class);
        when(options.getPrompt()).thenReturn(null);
        when(mockCompletions.getChoices()).thenReturn(Collections.emptyList());

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        Completions result = tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        assertThat(result).isSameAs(mockCompletions);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldHandleEmptyChoicesInCompletions() {
        // Arrange
        CompletionsOptions options = new CompletionsOptions(List.of("Test prompt"));
        when(mockCompletions.getChoices()).thenReturn(Collections.emptyList());

        when(mockOpenAIClient.getCompletions(anyString(), any(CompletionsOptions.class)))
            .thenReturn(mockCompletions);

        // Act
        Completions result = tracedClient.getCompletions(MODEL_NAME, options);

        // Assert
        assertThat(result).isSameAs(mockCompletions);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    // ========== unwrap Tests ==========

    @Test
    void shouldReturnUnwrappedClient() {
        // Act & Assert
        assertThat(tracedClient.unwrap()).isSameAs(mockOpenAIClient);
    }

    @Test
    void shouldReturnSameClientOnMultipleUnwrapCalls() {
        // Act & Assert
        assertThat(tracedClient.unwrap()).isSameAs(tracedClient.unwrap());
    }

    // ========== Constructor Tests ==========

    @Test
    void shouldCreateWithCustomTracer() {
        // Arrange & Act
        TracedAzureOpenAIClient client = new TracedAzureOpenAIClient(mockOpenAIClient, tracer);

        // Assert
        assertThat(client.unwrap()).isSameAs(mockOpenAIClient);
    }

    // ========== Helper Methods ==========

    private ChatCompletionsOptions createChatCompletionsOptions(String userMessage) {
        ChatRequestUserMessage msg = new ChatRequestUserMessage(userMessage);
        return new ChatCompletionsOptions(List.of(msg));
    }

    private void setupMockChatCompletions(String responseContent) {
        ChatChoice mockChoice = mock(ChatChoice.class);
        ChatResponseMessage mockMessage = mock(ChatResponseMessage.class);
        when(mockMessage.getRole()).thenReturn(ChatRole.ASSISTANT);
        when(mockMessage.getContent()).thenReturn(responseContent);
        when(mockChoice.getMessage()).thenReturn(mockMessage);
        when(mockChatCompletions.getChoices()).thenReturn(List.of(mockChoice));
    }

    private void setupMockCompletionsUsage(int prompt, int completion, int total) {
        when(mockCompletionsUsage.getPromptTokens()).thenReturn(prompt);
        when(mockCompletionsUsage.getCompletionTokens()).thenReturn(completion);
        when(mockCompletionsUsage.getTotalTokens()).thenReturn(total);
    }

    private void setupMockEmbeddings(int dimensions) {
        setupMockEmbeddings(dimensions, 1);
    }

    private void setupMockEmbeddings(int dimensions, int count) {
        List<EmbeddingItem> items = new java.util.ArrayList<>();
        for (int i = 0; i < count; i++) {
            EmbeddingItem mockItem = mock(EmbeddingItem.class);
            // Only stub getEmbedding() on the first item since the source
            // only checks firstEmbedding.getEmbedding().size() for dimensions
            if (i == 0) {
                List<Float> vector = new java.util.ArrayList<>();
                for (int j = 0; j < dimensions; j++) {
                    vector.add(0.0f);
                }
                when(mockItem.getEmbedding()).thenReturn(vector);
            }
            items.add(mockItem);
        }
        when(mockEmbeddings.getData()).thenReturn(items);
    }

    private void setupMockCompletions(String text) {
        Choice mockChoice = mock(Choice.class);
        when(mockChoice.getText()).thenReturn(text);
        when(mockCompletions.getChoices()).thenReturn(List.of(mockChoice));
    }
}
