package ai.traceai.openai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClient;
import com.openai.models.*;
import com.openai.services.blocking.ChatService;
import com.openai.services.blocking.EmbeddingService;
import com.openai.services.blocking.chat.CompletionService;
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

import java.util.Collections;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedOpenAIClient.
 *
 * <p>Note: OpenAI Java SDK uses Kotlin final classes that cannot be mocked.
 * We use real builder instances for model objects and only mock service interfaces.</p>
 */
@ExtendWith(MockitoExtension.class)
class TracedOpenAIClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private OpenAIClient mockClient;

    @Mock
    private ChatService mockChat;

    @Mock
    private CompletionService mockCompletions;

    @Mock
    private EmbeddingService mockEmbeddings;

    private FITracer tracer;
    private TracedOpenAIClient tracedClient;

    private static final String MODEL_NAME = "gpt-4";
    private static final String EMBEDDING_MODEL = "text-embedding-ada-002";
    private static final String RESPONSE_ID = "chatcmpl-12345";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedClient = new TracedOpenAIClient(mockClient, tracer);
    }

    // ========== createChatCompletion Tests ==========

    @Test
    void shouldCreateSpanForChatCompletion() {
        ChatCompletionCreateParams params = buildChatParams("Hello!");

        setupMockChatCompletionChain(buildChatCompletion("I'm doing well!"));

        ChatCompletion result = tracedClient.createChatCompletion(params);
        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("OpenAI Chat Completion");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("openai");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetModelAttributesForChatCompletion() {
        ChatCompletionCreateParams params = buildChatParams("Test");

        setupMockChatCompletionChain(buildChatCompletion("Response"));

        tracedClient.createChatCompletion(params);

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
    void shouldSetResponseAttributesForChatCompletion() {
        ChatCompletionCreateParams params = buildChatParams("Test");

        ChatCompletion completion = ChatCompletion.builder()
            .id(RESPONSE_ID)
            .model("gpt-4-0613")
            .created(System.currentTimeMillis() / 1000)
            .choices(List.of(
                ChatCompletion.Choice.builder()
                    .index(0)
                    .message(ChatCompletionMessage.builder()
                        .role(ChatCompletionMessage.Role.ASSISTANT)
                        .content("Response")
                        .build())
                    .finishReason(ChatCompletion.Choice.FinishReason.STOP)
                    .build()
            ))
            .build();

        setupMockChatCompletionChain(completion);

        tracedClient.createChatCompletion(params);

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
    void shouldSetTokenCountsForChatCompletion() {
        ChatCompletionCreateParams params = buildChatParams("Test");

        ChatCompletion completion = ChatCompletion.builder()
            .id("chatcmpl-1")
            .model(MODEL_NAME)
            .created(System.currentTimeMillis() / 1000)
            .choices(List.of(
                ChatCompletion.Choice.builder()
                    .index(0)
                    .message(ChatCompletionMessage.builder()
                        .role(ChatCompletionMessage.Role.ASSISTANT)
                        .content("Response")
                        .build())
                    .finishReason(ChatCompletion.Choice.FinishReason.STOP)
                    .build()
            ))
            .usage(CompletionUsage.builder()
                .promptTokens(10)
                .completionTokens(20)
                .totalTokens(30)
                .build())
            .build();

        setupMockChatCompletionChain(completion);

        tracedClient.createChatCompletion(params);

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
    void shouldRecordErrorOnChatCompletionFailure() {
        ChatCompletionCreateParams params = buildChatParams("Test");

        when(mockClient.chat()).thenReturn(mockChat);
        when(mockChat.completions()).thenReturn(mockCompletions);
        when(mockCompletions.create(any(ChatCompletionCreateParams.class)))
            .thenThrow(new RuntimeException("API connection error"));

        assertThatThrownBy(() -> tracedClient.createChatCompletion(params))
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
    void shouldCaptureOutputMessagesForChatCompletion() {
        ChatCompletionCreateParams params = buildChatParams("Hello!");

        String responseContent = "This is the response";
        setupMockChatCompletionChain(buildChatCompletion(responseContent));

        tracedClient.createChatCompletion(params);

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        String outputMessages = spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES)
        );
        assertThat(outputMessages).isNotNull();

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(responseContent);
    }

    @Test
    void shouldHandleEmptyChoicesInChatCompletion() {
        ChatCompletionCreateParams params = buildChatParams("Test");

        ChatCompletion completion = ChatCompletion.builder()
            .id("chatcmpl-1")
            .model(MODEL_NAME)
            .created(System.currentTimeMillis() / 1000)
            .choices(Collections.emptyList())
            .build();

        setupMockChatCompletionChain(completion);

        ChatCompletion result = tracedClient.createChatCompletion(params);
        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    // ========== createEmbedding Tests ==========

    @Test
    void shouldCreateSpanForEmbedding() {
        EmbeddingCreateParams params = EmbeddingCreateParams.builder()
            .model(EMBEDDING_MODEL)
            .input(EmbeddingCreateParams.Input.ofString("Hello, world!"))
            .build();

        setupMockEmbeddingChain(1536);

        CreateEmbeddingResponse result = tracedClient.createEmbedding(params);
        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("OpenAI Embedding");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("openai");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetEmbeddingModelName() {
        EmbeddingCreateParams params = EmbeddingCreateParams.builder()
            .model(EMBEDDING_MODEL)
            .input(EmbeddingCreateParams.Input.ofString("Test text"))
            .build();

        setupMockEmbeddingChain(1536);

        tracedClient.createEmbedding(params);

        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.EMBEDDING_MODEL_NAME)
        )).isEqualTo(EMBEDDING_MODEL);
    }

    @Test
    void shouldRecordErrorOnEmbeddingFailure() {
        EmbeddingCreateParams params = EmbeddingCreateParams.builder()
            .model(EMBEDDING_MODEL)
            .input(EmbeddingCreateParams.Input.ofString("Test text"))
            .build();

        when(mockClient.embeddings()).thenReturn(mockEmbeddings);
        when(mockEmbeddings.create(any(EmbeddingCreateParams.class)))
            .thenThrow(new RuntimeException("Embedding service unavailable"));

        assertThatThrownBy(() -> tracedClient.createEmbedding(params))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Embedding service unavailable");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
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

    // ========== Helper Methods ==========

    private ChatCompletionCreateParams buildChatParams(String content) {
        return ChatCompletionCreateParams.builder()
            .model(MODEL_NAME)
            .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                ChatCompletionUserMessageParam.builder()
                    .content(ChatCompletionUserMessageParam.Content.ofTextContent(content))
                    .build()
            ))
            .build();
    }

    private ChatCompletion buildChatCompletion(String responseContent) {
        return ChatCompletion.builder()
            .id("chatcmpl-1")
            .model(MODEL_NAME)
            .created(System.currentTimeMillis() / 1000)
            .choices(List.of(
                ChatCompletion.Choice.builder()
                    .index(0)
                    .message(ChatCompletionMessage.builder()
                        .role(ChatCompletionMessage.Role.ASSISTANT)
                        .content(responseContent)
                        .build())
                    .finishReason(ChatCompletion.Choice.FinishReason.STOP)
                    .build()
            ))
            .build();
    }

    private void setupMockChatCompletionChain(ChatCompletion completion) {
        when(mockClient.chat()).thenReturn(mockChat);
        when(mockChat.completions()).thenReturn(mockCompletions);
        when(mockCompletions.create(any(ChatCompletionCreateParams.class))).thenReturn(completion);
    }

    private void setupMockEmbeddingChain(int dimensions) {
        when(mockClient.embeddings()).thenReturn(mockEmbeddings);

        Embedding embeddingItem = Embedding.builder()
            .index(0)
            .embedding(Collections.nCopies(dimensions, 0.0))
            .build();

        CreateEmbeddingResponse response = CreateEmbeddingResponse.builder()
            .model(EMBEDDING_MODEL)
            .data(List.of(embeddingItem))
            .usage(CreateEmbeddingResponse.Usage.builder()
                .promptTokens(5)
                .totalTokens(5)
                .build())
            .build();

        when(mockEmbeddings.create(any(EmbeddingCreateParams.class))).thenReturn(response);
    }
}
