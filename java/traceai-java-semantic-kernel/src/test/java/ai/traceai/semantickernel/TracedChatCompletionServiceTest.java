package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.microsoft.semantickernel.services.chatcompletion.AuthorRole;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import reactor.core.publisher.Mono;

import java.util.Arrays;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TracedChatCompletionServiceTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private ChatCompletionService delegate;

    private FITracer tracer;
    private TracedChatCompletionService tracedService;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
        tracedService = new TracedChatCompletionService(delegate, "gpt-4", "openai", tracer);
    }

    @Test
    void shouldSetCorrectSpanKindForChatCompletion() {
        // Given
        ChatHistory history = new ChatHistory();
        history.addUserMessage("Hello");

        ChatMessageContent<?> responseMessage = ChatMessageContent.builder()
            .withAuthorRole(AuthorRole.ASSISTANT)
            .withContent("Hi there!")
            .build();

        when(delegate.getChatMessageContentsAsync(any(), any(), any()))
            .thenReturn(Mono.just(Arrays.asList(responseMessage)));

        // When
        tracedService.getChatMessageContentsAsync(history, null, null).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
    }

    @Test
    void shouldSetLLMAttributes() {
        // Given
        ChatHistory history = new ChatHistory();
        history.addUserMessage("What is AI?");

        ChatMessageContent<?> responseMessage = ChatMessageContent.builder()
            .withAuthorRole(AuthorRole.ASSISTANT)
            .withContent("AI stands for Artificial Intelligence.")
            .build();

        when(delegate.getChatMessageContentsAsync(any(), any(), any()))
            .thenReturn(Mono.just(Arrays.asList(responseMessage)));

        // When
        tracedService.getChatMessageContentsAsync(history, null, null).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)
        )).isEqualTo("semantic-kernel");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("openai");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo("gpt-4");
    }

    @Test
    void shouldCaptureInputMessages() {
        // Given
        ChatHistory history = new ChatHistory();
        history.addSystemMessage("You are a helpful assistant.");
        history.addUserMessage("Hello!");

        ChatMessageContent<?> responseMessage = ChatMessageContent.builder()
            .withAuthorRole(AuthorRole.ASSISTANT)
            .withContent("Hi!")
            .build();

        when(delegate.getChatMessageContentsAsync(any(), any(), any()))
            .thenReturn(Mono.just(Arrays.asList(responseMessage)));

        // When
        tracedService.getChatMessageContentsAsync(history, null, null).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.input_messages.0.message.role")
        )).isEqualTo("system");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.input_messages.0.message.content")
        )).isEqualTo("You are a helpful assistant.");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.input_messages.1.message.role")
        )).isEqualTo("user");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.input_messages.1.message.content")
        )).isEqualTo("Hello!");
    }

    @Test
    void shouldCaptureOutputMessages() {
        // Given
        ChatHistory history = new ChatHistory();
        history.addUserMessage("Hello!");

        ChatMessageContent<?> responseMessage = ChatMessageContent.builder()
            .withAuthorRole(AuthorRole.ASSISTANT)
            .withContent("Hello! How can I help you today?")
            .build();

        when(delegate.getChatMessageContentsAsync(any(), any(), any()))
            .thenReturn(Mono.just(Arrays.asList(responseMessage)));

        // When
        tracedService.getChatMessageContentsAsync(history, null, null).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.output_messages.0.message.role")
        )).isEqualTo("assistant");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("llm.output_messages.0.message.content")
        )).isEqualTo("Hello! How can I help you today?");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo("Hello! How can I help you today?");
    }

    @Test
    void shouldHandleErrors() {
        // Given
        ChatHistory history = new ChatHistory();
        history.addUserMessage("Hello!");

        RuntimeException error = new RuntimeException("API Error");
        when(delegate.getChatMessageContentsAsync(any(), any(), any()))
            .thenReturn(Mono.error(error));

        // When/Then
        try {
            tracedService.getChatMessageContentsAsync(history, null, null).block();
        } catch (Exception e) {
            // Expected
        }

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void shouldUnwrapToOriginalService() {
        assertThat(tracedService.unwrap()).isSameAs(delegate);
    }

    @Test
    void shouldReturnTracer() {
        assertThat(tracedService.getTracer()).isSameAs(tracer);
    }

    @Test
    void shouldDelegateGetModelId() {
        when(delegate.getModelId()).thenReturn("gpt-4");
        assertThat(tracedService.getModelId()).isEqualTo("gpt-4");
    }

    @Test
    void shouldDelegateGetServiceId() {
        when(delegate.getServiceId()).thenReturn("my-service");
        assertThat(tracedService.getServiceId()).isEqualTo("my-service");
    }
}
