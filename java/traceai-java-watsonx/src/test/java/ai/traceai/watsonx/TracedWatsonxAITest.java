package ai.traceai.watsonx;

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

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedWatsonxAI.
 */
@ExtendWith(MockitoExtension.class)
class TracedWatsonxAITest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    private FITracer tracer;
    private TracedWatsonxAI tracedWatsonx;
    private MockWatsonxClient mockClient;

    @BeforeEach
    void setUp() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        mockClient = new MockWatsonxClient();
        tracedWatsonx = new TracedWatsonxAI(mockClient, tracer);
    }

    // ==================== generateText Tests ====================

    @Test
    void testGenerateText_createsSpanWithCorrectAttributes() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Hello, how are you?");
        request.setProjectId("test-project-123");

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("I'm doing well, thank you!");
        response.setInputTokenCount(5);
        response.setGeneratedTokenCount(6);
        response.setStopReason("end_of_sequence");
        mockClient.setTextGenResponse(response);

        // When
        MockTextGenResponse result = tracedWatsonx.generateText(request);

        // Then
        assertThat(result).isNotNull();
        assertThat(result.getGeneratedText()).isEqualTo("I'm doing well, thank you!");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Watsonx Text Generation");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);

        // Verify semantic convention attributes
        // Note: LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // so the last setAttribute call wins. Source sets LLM_PROVIDER="ibm" after LLM_SYSTEM="watsonx".
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)))
            .isEqualTo("ibm");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)))
            .isEqualTo("ibm/granite-13b-chat-v2");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)))
            .isEqualTo("ibm/granite-13b-chat-v2");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("watsonx.project_id")))
            .isEqualTo("test-project-123");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.LLM.getValue());
    }

    @Test
    void testGenerateText_capturesInputAndOutput() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Translate hello to Spanish");

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Hola");
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)))
            .isEqualTo("Translate hello to Spanish");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo("Hola");
    }

    @Test
    void testGenerateText_capturesTokenCounts() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Write a poem");

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Roses are red...");
        response.setInputTokenCount(3);
        response.setGeneratedTokenCount(4);
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)))
            .isEqualTo(3L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)))
            .isEqualTo(4L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
            .isEqualTo(7L);
    }

    @Test
    void testGenerateText_capturesStopReason() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Test input");

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Test output");
        response.setStopReason("max_tokens");
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON)))
            .isEqualTo("max_tokens");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("watsonx.stop_reason")))
            .isEqualTo("max_tokens");
    }

    @Test
    void testGenerateText_withParameters() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Hello");

        MockTextGenParameters params = new MockTextGenParameters();
        params.setTemperature(0.7);
        params.setMaxNewTokens(100);
        params.setTopP(0.9);
        params.setStopSequences(Arrays.asList(".", "?", "!"));
        request.setParameters(params);

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Hi there!");
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)))
            .isEqualTo(0.7);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_REQUEST_MAX_TOKENS)))
            .isEqualTo(100L);
        assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TOP_P)))
            .isEqualTo(0.9);
    }

    @Test
    void testGenerateText_withSpaceId() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Hello");
        request.setSpaceId("space-456");

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Hi!");
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey("watsonx.space_id")))
            .isEqualTo("space-456");
    }

    @Test
    void testGenerateText_handlesError() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Hello");
        mockClient.setShouldThrowError(true);
        mockClient.setErrorMessage("API rate limit exceeded");

        // When/Then
        assertThatThrownBy(() -> tracedWatsonx.generateText(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Watsonx text generation failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        // The mock client throws via reflection (invokeMethod), so the exception is wrapped
        // in InvocationTargetException before being caught by the source's catch block.
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)))
            .isEqualTo("java.lang.reflect.InvocationTargetException");
    }

    @Test
    void testGenerateText_withResultsList() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Test");

        MockTextGenResponseWithResults response = new MockTextGenResponseWithResults();
        MockTextGenResult result1 = new MockTextGenResult();
        result1.setGeneratedText("Result text");
        result1.setStopReason("completed");
        result1.setInputTokenCount(2);
        result1.setGeneratedTokenCount(3);
        response.setResults(Arrays.asList(result1));
        mockClient.setTextGenResponseWithResults(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo("Result text");
    }

    // ==================== generateTextStream Tests ====================

    @Test
    void testGenerateTextStream_createsSpanWithCorrectName() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Stream this text");
        request.setProjectId("test-project");

        mockClient.setStreamResponse(Arrays.asList("chunk1", "chunk2", "chunk3").iterator());

        // When
        tracedWatsonx.generateTextStream(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Watsonx Text Generation (Stream)");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);

        // LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // last setAttribute call wins (LLM_PROVIDER="ibm")
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)))
            .isEqualTo("ibm");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)))
            .isEqualTo("ibm/granite-13b-chat-v2");
    }

    @Test
    void testGenerateTextStream_capturesInput() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Streaming input text");

        mockClient.setStreamResponse(Arrays.asList("output").iterator());

        // When
        tracedWatsonx.generateTextStream(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)))
            .isEqualTo("Streaming input text");
    }

    @Test
    void testGenerateTextStream_withParameters() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Test");

        MockTextGenParameters params = new MockTextGenParameters();
        params.setTemperature(0.5);
        params.setMaxNewTokens(50);
        request.setParameters(params);

        mockClient.setStreamResponse(Arrays.asList("chunk").iterator());

        // When
        tracedWatsonx.generateTextStream(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)))
            .isEqualTo(0.5);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_REQUEST_MAX_TOKENS)))
            .isEqualTo(50L);
    }

    @Test
    void testGenerateTextStream_handlesError() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput("Hello");
        mockClient.setShouldThrowError(true);
        mockClient.setErrorMessage("Stream error");

        // When/Then
        assertThatThrownBy(() -> tracedWatsonx.generateTextStream(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Watsonx streaming text generation failed");

        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ==================== chat Tests ====================

    @Test
    void testChat_createsSpanWithCorrectAttributes() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setProjectId("chat-project");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("What is the capital of France?");
        request.setMessages(Arrays.asList(userMessage));

        MockChatResponse response = new MockChatResponse();
        response.setId("chat-response-123");
        MockChatChoice choice = new MockChatChoice();
        MockChatMessage assistantMessage = new MockChatMessage();
        assistantMessage.setRole("assistant");
        assistantMessage.setContent("The capital of France is Paris.");
        choice.setMessage(assistantMessage);
        choice.setFinishReason("stop");
        response.setChoices(Arrays.asList(choice));

        MockChatUsage usage = new MockChatUsage();
        usage.setPromptTokens(8);
        usage.setCompletionTokens(7);
        response.setUsage(usage);

        mockClient.setChatResponse(response);

        // When
        MockChatResponse result = tracedWatsonx.chat(request);

        // Then
        assertThat(result).isNotNull();
        assertThat(result.getId()).isEqualTo("chat-response-123");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Watsonx Chat");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);

        // LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // last setAttribute call wins (LLM_PROVIDER="ibm")
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)))
            .isEqualTo("ibm");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)))
            .isEqualTo("ibm/granite-13b-chat-v2");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.LLM.getValue());
    }

    @Test
    void testChat_capturesMessages() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");

        MockChatMessage systemMessage = new MockChatMessage();
        systemMessage.setRole("system");
        systemMessage.setContent("You are a helpful assistant.");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("Hello!");

        request.setMessages(Arrays.asList(systemMessage, userMessage));

        MockChatResponse response = new MockChatResponse();
        MockChatChoice choice = new MockChatChoice();
        MockChatMessage assistantMessage = new MockChatMessage();
        assistantMessage.setRole("assistant");
        assistantMessage.setContent("Hi there!");
        choice.setMessage(assistantMessage);
        response.setChoices(Arrays.asList(choice));
        mockClient.setChatResponse(response);

        // When
        tracedWatsonx.chat(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        // Check input messages (JSON blob format under gen_ai.input.messages)
        String inputMessages = span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES));
        assertThat(inputMessages).isNotNull();
        assertThat(inputMessages).contains("\"role\":\"system\"");
        assertThat(inputMessages).contains("\"content\":\"You are a helpful assistant.\"");
        assertThat(inputMessages).contains("\"role\":\"user\"");
        assertThat(inputMessages).contains("\"content\":\"Hello!\"");

        // Check output messages (JSON blob format under gen_ai.output.messages)
        String outputMessages = span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES));
        assertThat(outputMessages).isNotNull();
        assertThat(outputMessages).contains("\"role\":\"assistant\"");
        assertThat(outputMessages).contains("\"content\":\"Hi there!\"");

        // Check input value is set to last user message
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)))
            .isEqualTo("Hello!");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo("Hi there!");
    }

    @Test
    void testChat_capturesTokenUsage() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("Test");
        request.setMessages(Arrays.asList(userMessage));

        MockChatResponse response = new MockChatResponse();
        MockChatChoice choice = new MockChatChoice();
        MockChatMessage assistantMessage = new MockChatMessage();
        assistantMessage.setRole("assistant");
        assistantMessage.setContent("Response");
        choice.setMessage(assistantMessage);
        response.setChoices(Arrays.asList(choice));

        MockChatUsage usage = new MockChatUsage();
        usage.setPromptTokens(10);
        usage.setCompletionTokens(20);
        response.setUsage(usage);
        mockClient.setChatResponse(response);

        // When
        tracedWatsonx.chat(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)))
            .isEqualTo(20L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
            .isEqualTo(30L);
    }

    @Test
    void testChat_capturesFinishReason() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("Test");
        request.setMessages(Arrays.asList(userMessage));

        MockChatResponse response = new MockChatResponse();
        MockChatChoice choice = new MockChatChoice();
        MockChatMessage assistantMessage = new MockChatMessage();
        assistantMessage.setRole("assistant");
        assistantMessage.setContent("Response");
        choice.setMessage(assistantMessage);
        choice.setFinishReason("length");
        response.setChoices(Arrays.asList(choice));
        mockClient.setChatResponse(response);

        // When
        tracedWatsonx.chat(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON)))
            .isEqualTo("length");
    }

    @Test
    void testChat_capturesResponseId() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("Test");
        request.setMessages(Arrays.asList(userMessage));

        MockChatResponse response = new MockChatResponse();
        response.setId("resp-abc-123");
        MockChatChoice choice = new MockChatChoice();
        MockChatMessage assistantMessage = new MockChatMessage();
        assistantMessage.setRole("assistant");
        assistantMessage.setContent("Response");
        choice.setMessage(assistantMessage);
        response.setChoices(Arrays.asList(choice));
        mockClient.setChatResponse(response);

        // When
        tracedWatsonx.chat(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_ID)))
            .isEqualTo("resp-abc-123");
    }

    @Test
    void testChat_withParameters() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("Test");
        request.setMessages(Arrays.asList(userMessage));

        MockChatParameters params = new MockChatParameters();
        params.setTemperature(0.8);
        params.setMaxTokens(200);
        params.setTopP(0.95);
        request.setParameters(params);

        MockChatResponse response = new MockChatResponse();
        MockChatChoice choice = new MockChatChoice();
        MockChatMessage assistantMessage = new MockChatMessage();
        assistantMessage.setRole("assistant");
        assistantMessage.setContent("Response");
        choice.setMessage(assistantMessage);
        response.setChoices(Arrays.asList(choice));
        mockClient.setChatResponse(response);

        // When
        tracedWatsonx.chat(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE)))
            .isEqualTo(0.8);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_REQUEST_MAX_TOKENS)))
            .isEqualTo(200L);
        assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TOP_P)))
            .isEqualTo(0.95);
    }

    @Test
    void testChat_handlesError() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");

        MockChatMessage userMessage = new MockChatMessage();
        userMessage.setRole("user");
        userMessage.setContent("Test");
        request.setMessages(Arrays.asList(userMessage));

        mockClient.setShouldThrowError(true);
        mockClient.setErrorMessage("Chat service unavailable");

        // When/Then
        assertThatThrownBy(() -> tracedWatsonx.chat(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Watsonx chat failed");

        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        // The mock client throws via reflection (invokeMethod), so the exception is wrapped
        // in InvocationTargetException. Its getMessage() returns the target exception's toString().
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)))
            .isEqualTo("java.lang.reflect.InvocationTargetException");
    }

    // ==================== embedText Tests ====================

    @Test
    void testEmbedText_createsSpanWithCorrectAttributes() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setProjectId("embed-project");
        request.setInputs(Arrays.asList("Hello world", "How are you?"));

        MockEmbedResponse response = new MockEmbedResponse();
        MockEmbedResult result1 = new MockEmbedResult();
        result1.setEmbedding(createEmbeddingVector(384));
        MockEmbedResult result2 = new MockEmbedResult();
        result2.setEmbedding(createEmbeddingVector(384));
        response.setResults(Arrays.asList(result1, result2));
        response.setInputTokenCount(6);
        mockClient.setEmbedResponse(response);

        // When
        MockEmbedResponse result = tracedWatsonx.embedText(request);

        // Then
        assertThat(result).isNotNull();
        assertThat(result.getResults()).hasSize(2);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Watsonx Embed");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);

        // LLM_SYSTEM and LLM_PROVIDER both resolve to "gen_ai.provider.name",
        // last setAttribute call wins (LLM_PROVIDER="ibm")
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)))
            .isEqualTo("ibm");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.EMBEDDING_MODEL_NAME)))
            .isEqualTo("ibm/slate-30m-english-rtrvr");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey("watsonx.project_id")))
            .isEqualTo("embed-project");
    }

    @Test
    void testEmbedText_capturesVectorCountAndDimensions() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setInputs(Arrays.asList("Text 1", "Text 2", "Text 3"));

        MockEmbedResponse response = new MockEmbedResponse();
        List<MockEmbedResult> results = new ArrayList<>();
        for (int i = 0; i < 3; i++) {
            MockEmbedResult r = new MockEmbedResult();
            r.setEmbedding(createEmbeddingVector(768));
            results.add(r);
        }
        response.setResults(results);
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT)))
            .isEqualTo(3L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(768L);
    }

    @Test
    void testEmbedText_capturesInputTokenCount() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setInputs(Arrays.asList("Test input"));

        MockEmbedResponse response = new MockEmbedResponse();
        MockEmbedResult result = new MockEmbedResult();
        result.setEmbedding(createEmbeddingVector(384));
        response.setResults(Arrays.asList(result));
        response.setInputTokenCount(2);
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)))
            .isEqualTo(2L);
    }

    @Test
    void testEmbedText_capturesInputValue() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setInputs(Arrays.asList("First text", "Second text"));

        MockEmbedResponse response = new MockEmbedResponse();
        response.setResults(new ArrayList<>());
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        String inputValue = span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE));
        assertThat(inputValue).contains("First text");
        assertThat(inputValue).contains("Second text");
    }

    @Test
    void testEmbedText_truncatesLargeInputList() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        List<String> inputs = new ArrayList<>();
        for (int i = 0; i < 10; i++) {
            inputs.add("Text " + i);
        }
        request.setInputs(inputs);

        MockEmbedResponse response = new MockEmbedResponse();
        response.setResults(new ArrayList<>());
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        String inputValue = span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE));
        assertThat(inputValue).contains("... and 5 more");
    }

    @Test
    void testEmbedText_withSpaceId() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setSpaceId("embed-space-123");
        request.setInputs(Arrays.asList("Test"));

        MockEmbedResponse response = new MockEmbedResponse();
        response.setResults(new ArrayList<>());
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.stringKey("watsonx.space_id")))
            .isEqualTo("embed-space-123");
    }

    @Test
    void testEmbedText_withTruncateInputTokens() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setInputs(Arrays.asList("Test"));
        request.setTruncateInputTokens(512);

        MockEmbedResponse response = new MockEmbedResponse();
        response.setResults(new ArrayList<>());
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        assertThat(span.getAttributes().get(AttributeKey.longKey("watsonx.truncate_input_tokens")))
            .isEqualTo(512L);
    }

    @Test
    void testEmbedText_handlesError() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setInputs(Arrays.asList("Test"));

        mockClient.setShouldThrowError(true);
        mockClient.setErrorMessage("Embedding service error");

        // When/Then
        assertThatThrownBy(() -> tracedWatsonx.embedText(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Watsonx embed failed");

        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ==================== unwrap Tests ====================

    @Test
    void testUnwrap_returnsOriginalClient() {
        // When
        Object unwrapped = tracedWatsonx.unwrap();

        // Then
        assertThat(unwrapped).isSameAs(mockClient);
    }

    // ==================== Constructor Tests ====================

    @Test
    void testConstructorWithClientOnly() {
        // This test would require TraceAI.getTracer() to return a valid tracer
        // In a real scenario, you would need to initialize TraceAI first
        // For now, we just verify the constructor with explicit tracer works
        TracedWatsonxAI traced = new TracedWatsonxAI(mockClient, tracer);
        assertThat(traced.unwrap()).isSameAs(mockClient);
    }

    // ==================== Edge Cases ====================

    @Test
    void testGenerateText_withNullModelId() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId(null);
        request.setInput("Hello");

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Hi");
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        // Model should not be set if null
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)))
            .isNull();
    }

    @Test
    void testGenerateText_withNullInput() {
        // Given
        MockTextGenRequest request = new MockTextGenRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setInput(null);

        MockTextGenResponse response = new MockTextGenResponse();
        response.setGeneratedText("Response");
        mockClient.setTextGenResponse(response);

        // When
        tracedWatsonx.generateText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData span = spans.get(0);

        // Input should not be set if null
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)))
            .isNull();
    }

    @Test
    void testChat_withEmptyMessagesList() {
        // Given
        MockChatRequest request = new MockChatRequest();
        request.setModelId("ibm/granite-13b-chat-v2");
        request.setMessages(new ArrayList<>());

        MockChatResponse response = new MockChatResponse();
        response.setChoices(new ArrayList<>());
        mockClient.setChatResponse(response);

        // When
        tracedWatsonx.chat(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void testEmbedText_withEmptyInputsList() {
        // Given
        MockEmbedRequest request = new MockEmbedRequest();
        request.setModelId("ibm/slate-30m-english-rtrvr");
        request.setInputs(new ArrayList<>());

        MockEmbedResponse response = new MockEmbedResponse();
        response.setResults(new ArrayList<>());
        mockClient.setEmbedResponse(response);

        // When
        tracedWatsonx.embedText(request);

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    // ==================== Helper Methods ====================

    private static List<Double> createEmbeddingVector(int size) {
        List<Double> vector = new ArrayList<>();
        for (int i = 0; i < size; i++) {
            vector.add(0.0);
        }
        return vector;
    }

    // ==================== Mock Classes ====================

    /**
     * Mock WatsonxAI client for testing.
     */
    static class MockWatsonxClient {
        private MockTextGenResponse textGenResponse;
        private MockTextGenResponseWithResults textGenResponseWithResults;
        private Object streamResponse;
        private MockChatResponse chatResponse;
        private MockEmbedResponse embedResponse;
        private boolean shouldThrowError = false;
        private String errorMessage = "Mock error";

        public void setTextGenResponse(MockTextGenResponse response) {
            this.textGenResponse = response;
        }

        public void setTextGenResponseWithResults(MockTextGenResponseWithResults response) {
            this.textGenResponseWithResults = response;
        }

        public void setStreamResponse(Object response) {
            this.streamResponse = response;
        }

        public void setChatResponse(MockChatResponse response) {
            this.chatResponse = response;
        }

        public void setEmbedResponse(MockEmbedResponse response) {
            this.embedResponse = response;
        }

        public void setShouldThrowError(boolean shouldThrow) {
            this.shouldThrowError = shouldThrow;
        }

        public void setErrorMessage(String message) {
            this.errorMessage = message;
        }

        public Object generateText(Object request) {
            if (shouldThrowError) {
                throw new RuntimeException(errorMessage);
            }
            return textGenResponseWithResults != null ? textGenResponseWithResults : textGenResponse;
        }

        public Object generateTextStream(Object request) {
            if (shouldThrowError) {
                throw new RuntimeException(errorMessage);
            }
            return streamResponse;
        }

        public Object chat(Object request) {
            if (shouldThrowError) {
                throw new RuntimeException(errorMessage);
            }
            return chatResponse;
        }

        public Object embedText(Object request) {
            if (shouldThrowError) {
                throw new RuntimeException(errorMessage);
            }
            return embedResponse;
        }
    }

    // Text Generation Request/Response Mocks
    static class MockTextGenRequest {
        private String modelId;
        private String input;
        private String projectId;
        private String spaceId;
        private MockTextGenParameters parameters;

        public String getModelId() { return modelId; }
        public void setModelId(String modelId) { this.modelId = modelId; }
        public String getInput() { return input; }
        public void setInput(String input) { this.input = input; }
        public String getProjectId() { return projectId; }
        public void setProjectId(String projectId) { this.projectId = projectId; }
        public String getSpaceId() { return spaceId; }
        public void setSpaceId(String spaceId) { this.spaceId = spaceId; }
        public MockTextGenParameters getParameters() { return parameters; }
        public void setParameters(MockTextGenParameters parameters) { this.parameters = parameters; }
    }

    static class MockTextGenParameters {
        private Double temperature;
        private Integer maxNewTokens;
        private Double topP;
        private List<String> stopSequences;

        public Double getTemperature() { return temperature; }
        public void setTemperature(Double temperature) { this.temperature = temperature; }
        public Integer getMaxNewTokens() { return maxNewTokens; }
        public void setMaxNewTokens(Integer maxNewTokens) { this.maxNewTokens = maxNewTokens; }
        public Double getTopP() { return topP; }
        public void setTopP(Double topP) { this.topP = topP; }
        public List<String> getStopSequences() { return stopSequences; }
        public void setStopSequences(List<String> stopSequences) { this.stopSequences = stopSequences; }
    }

    static class MockTextGenResponse {
        private String generatedText;
        private Integer inputTokenCount;
        private Integer generatedTokenCount;
        private String stopReason;
        private String modelVersion;

        public String getGeneratedText() { return generatedText; }
        public void setGeneratedText(String generatedText) { this.generatedText = generatedText; }
        public Integer getInputTokenCount() { return inputTokenCount; }
        public void setInputTokenCount(Integer inputTokenCount) { this.inputTokenCount = inputTokenCount; }
        public Integer getGeneratedTokenCount() { return generatedTokenCount; }
        public void setGeneratedTokenCount(Integer generatedTokenCount) { this.generatedTokenCount = generatedTokenCount; }
        public String getStopReason() { return stopReason; }
        public void setStopReason(String stopReason) { this.stopReason = stopReason; }
        public String getModelVersion() { return modelVersion; }
        public void setModelVersion(String modelVersion) { this.modelVersion = modelVersion; }
    }

    static class MockTextGenResponseWithResults {
        private List<MockTextGenResult> results;

        public List<MockTextGenResult> getResults() { return results; }
        public void setResults(List<MockTextGenResult> results) { this.results = results; }
    }

    static class MockTextGenResult {
        private String generatedText;
        private String stopReason;
        private Integer inputTokenCount;
        private Integer generatedTokenCount;

        public String getGeneratedText() { return generatedText; }
        public void setGeneratedText(String generatedText) { this.generatedText = generatedText; }
        public String getStopReason() { return stopReason; }
        public void setStopReason(String stopReason) { this.stopReason = stopReason; }
        public Integer getInputTokenCount() { return inputTokenCount; }
        public void setInputTokenCount(Integer inputTokenCount) { this.inputTokenCount = inputTokenCount; }
        public Integer getGeneratedTokenCount() { return generatedTokenCount; }
        public void setGeneratedTokenCount(Integer generatedTokenCount) { this.generatedTokenCount = generatedTokenCount; }
    }

    // Chat Request/Response Mocks
    static class MockChatRequest {
        private String modelId;
        private String projectId;
        private String spaceId;
        private List<MockChatMessage> messages;
        private MockChatParameters parameters;

        public String getModelId() { return modelId; }
        public void setModelId(String modelId) { this.modelId = modelId; }
        public String getProjectId() { return projectId; }
        public void setProjectId(String projectId) { this.projectId = projectId; }
        public String getSpaceId() { return spaceId; }
        public void setSpaceId(String spaceId) { this.spaceId = spaceId; }
        public List<MockChatMessage> getMessages() { return messages; }
        public void setMessages(List<MockChatMessage> messages) { this.messages = messages; }
        public MockChatParameters getParameters() { return parameters; }
        public void setParameters(MockChatParameters parameters) { this.parameters = parameters; }
    }

    static class MockChatMessage {
        private String role;
        private String content;

        public String getRole() { return role; }
        public void setRole(String role) { this.role = role; }
        public String getContent() { return content; }
        public void setContent(String content) { this.content = content; }
    }

    static class MockChatParameters {
        private Double temperature;
        private Integer maxTokens;
        private Double topP;

        public Double getTemperature() { return temperature; }
        public void setTemperature(Double temperature) { this.temperature = temperature; }
        public Integer getMaxTokens() { return maxTokens; }
        public void setMaxTokens(Integer maxTokens) { this.maxTokens = maxTokens; }
        public Double getTopP() { return topP; }
        public void setTopP(Double topP) { this.topP = topP; }
    }

    static class MockChatResponse {
        private String id;
        private List<MockChatChoice> choices;
        private MockChatUsage usage;

        public String getId() { return id; }
        public void setId(String id) { this.id = id; }
        public List<MockChatChoice> getChoices() { return choices; }
        public void setChoices(List<MockChatChoice> choices) { this.choices = choices; }
        public MockChatUsage getUsage() { return usage; }
        public void setUsage(MockChatUsage usage) { this.usage = usage; }
    }

    static class MockChatChoice {
        private MockChatMessage message;
        private String finishReason;

        public MockChatMessage getMessage() { return message; }
        public void setMessage(MockChatMessage message) { this.message = message; }
        public String getFinishReason() { return finishReason; }
        public void setFinishReason(String finishReason) { this.finishReason = finishReason; }
    }

    static class MockChatUsage {
        private Integer promptTokens;
        private Integer completionTokens;

        public Integer getPromptTokens() { return promptTokens; }
        public void setPromptTokens(Integer promptTokens) { this.promptTokens = promptTokens; }
        public Integer getCompletionTokens() { return completionTokens; }
        public void setCompletionTokens(Integer completionTokens) { this.completionTokens = completionTokens; }
    }

    // Embed Request/Response Mocks
    static class MockEmbedRequest {
        private String modelId;
        private String projectId;
        private String spaceId;
        private List<String> inputs;
        private Integer truncateInputTokens;

        public String getModelId() { return modelId; }
        public void setModelId(String modelId) { this.modelId = modelId; }
        public String getProjectId() { return projectId; }
        public void setProjectId(String projectId) { this.projectId = projectId; }
        public String getSpaceId() { return spaceId; }
        public void setSpaceId(String spaceId) { this.spaceId = spaceId; }
        public List<String> getInputs() { return inputs; }
        public void setInputs(List<String> inputs) { this.inputs = inputs; }
        public Integer getTruncateInputTokens() { return truncateInputTokens; }
        public void setTruncateInputTokens(Integer truncateInputTokens) { this.truncateInputTokens = truncateInputTokens; }
    }

    static class MockEmbedResponse {
        private List<MockEmbedResult> results;
        private Integer inputTokenCount;

        public List<MockEmbedResult> getResults() { return results; }
        public void setResults(List<MockEmbedResult> results) { this.results = results; }
        public Integer getInputTokenCount() { return inputTokenCount; }
        public void setInputTokenCount(Integer inputTokenCount) { this.inputTokenCount = inputTokenCount; }
    }

    static class MockEmbedResult {
        private List<Double> embedding;

        public List<Double> getEmbedding() { return embedding; }
        public void setEmbedding(List<Double> embedding) { this.embedding = embedding; }
    }
}
