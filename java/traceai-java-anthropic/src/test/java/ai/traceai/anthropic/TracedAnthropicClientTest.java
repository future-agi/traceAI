package ai.traceai.anthropic;

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
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Unit tests for TracedAnthropicClient.
 *
 * <p>TracedAnthropicClient uses reflection to interact with the Anthropic SDK,
 * so we use simple POJOs that expose the same methods/fields the reflection
 * helpers look for (messages(), create(), model(), id(), content, etc.).</p>
 */
@ExtendWith(MockitoExtension.class)
class TracedAnthropicClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    private FITracer tracer;
    private TracedAnthropicClient tracedClient;
    private StubAnthropicClient stubClient;

    private static final String MODEL_NAME = "claude-3-5-sonnet-20241022";
    private static final String RESPONSE_ID = "msg_12345";

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        stubClient = new StubAnthropicClient();
        tracedClient = new TracedAnthropicClient(stubClient, tracer);
    }

    // ========== createMessage Tests ==========

    @Test
    void shouldCreateSpanForMessage() {
        // Arrange
        StubMessageParams params = new StubMessageParams(MODEL_NAME, 1024, "Hello!");
        stubClient.messagesApi.nextResponse = new StubMessageResponse(
            RESPONSE_ID, MODEL_NAME, "Hello! How can I help?", "end_turn"
        );

        // Act
        Object result = tracedClient.createMessage(params);

        // Assert
        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Anthropic Message");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("anthropic");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldSetModelAttributesForMessage() {
        // Arrange
        StubMessageParams params = new StubMessageParams(MODEL_NAME, 1024, "Test");
        stubClient.messagesApi.nextResponse = new StubMessageResponse(
            RESPONSE_ID, MODEL_NAME, "Response", "end_turn"
        );

        // Act
        tracedClient.createMessage(params);

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
    void shouldSetResponseIdForMessage() {
        // Arrange
        StubMessageParams params = new StubMessageParams(MODEL_NAME, 1024, "Test");
        stubClient.messagesApi.nextResponse = new StubMessageResponse(
            RESPONSE_ID, MODEL_NAME, "Response", "end_turn"
        );

        // Act
        tracedClient.createMessage(params);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_ID)
        )).isEqualTo(RESPONSE_ID);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_MODEL)
        )).isEqualTo(MODEL_NAME);
    }

    @Test
    void shouldRecordErrorOnMessageFailure() {
        // Arrange
        StubMessageParams params = new StubMessageParams(MODEL_NAME, 1024, "Test");
        stubClient.messagesApi.shouldThrow = true;
        stubClient.messagesApi.throwMessage = "API rate limit exceeded";

        // Act & Assert
        // TracedAnthropicClient wraps exceptions in RuntimeException("Failed to create message", e)
        assertThatThrownBy(() -> tracedClient.createMessage(params))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Failed to create message");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldCaptureOutputValueForMessage() {
        // Arrange
        String expectedOutput = "This is the assistant response";
        StubMessageParams params = new StubMessageParams(MODEL_NAME, 1024, "Hello!");
        stubClient.messagesApi.nextResponse = new StubMessageResponse(
            RESPONSE_ID, MODEL_NAME, expectedOutput, "end_turn"
        );

        // Act
        tracedClient.createMessage(params);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);
    }

    // ========== unwrap Tests ==========

    @Test
    void shouldReturnUnwrappedClient() {
        assertThat(tracedClient.unwrap()).isSameAs(stubClient);
    }

    @Test
    void shouldReturnSameClientOnMultipleUnwrapCalls() {
        assertThat(tracedClient.unwrap()).isSameAs(tracedClient.unwrap());
    }

    // ========== Stub Classes for Reflection ==========

    /**
     * Stub Anthropic client that exposes a messages() method
     * compatible with the reflection-based TracedAnthropicClient.
     */
    static class StubAnthropicClient {
        final StubMessagesApi messagesApi = new StubMessagesApi();

        public StubMessagesApi messages() {
            return messagesApi;
        }
    }

    /**
     * Stub messages API that exposes a create(params) method.
     */
    static class StubMessagesApi {
        StubMessageResponse nextResponse;
        boolean shouldThrow = false;
        String throwMessage = "API error";

        public StubMessageResponse create(Object params) {
            if (shouldThrow) {
                throw new RuntimeException(throwMessage);
            }
            return nextResponse;
        }
    }

    /**
     * Stub message creation parameters with fields the reflection
     * helpers can extract (model, maxTokens, messages).
     */
    static class StubMessageParams {
        private final String model;
        private final int maxTokens;
        private final List<StubUserMessage> messages;

        StubMessageParams(String model, int maxTokens, String userMessage) {
            this.model = model;
            this.maxTokens = maxTokens;
            this.messages = List.of(new StubUserMessage(userMessage));
        }

        public String getModel() { return model; }
        public int getMaxTokens() { return maxTokens; }
        public List<StubUserMessage> getMessages() { return messages; }
    }

    /**
     * Stub user message with role and content fields.
     */
    static class StubUserMessage {
        private final String role = "user";
        private final String content;

        StubUserMessage(String content) {
            this.content = content;
        }

        public String getRole() { return role; }
        public String getContent() { return content; }
    }

    /**
     * Stub message response with id, model, content blocks, and stop reason.
     */
    static class StubMessageResponse {
        private final String id;
        private final String model;
        private final List<StubContentBlock> content;
        private final String stopReason;

        StubMessageResponse(String id, String model, String text, String stopReason) {
            this.id = id;
            this.model = model;
            this.content = List.of(new StubContentBlock(text));
            this.stopReason = stopReason;
        }

        public String getId() { return id; }
        public String getModel() { return model; }
        public List<StubContentBlock> getContent() { return content; }
        public String getStopReason() { return stopReason; }
    }

    /**
     * Stub content block with a text field.
     */
    static class StubContentBlock {
        private final String text;

        StubContentBlock(String text) {
            this.text = text;
        }

        public String getText() { return text; }
    }
}
