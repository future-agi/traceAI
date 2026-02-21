package ai.traceai.cohere;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.cohere.api.Cohere;
import com.cohere.api.requests.ChatRequest;
import com.cohere.api.requests.EmbedRequest;
import com.cohere.api.requests.RerankRequest;
import com.cohere.api.types.*;
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
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedCohereClient.
 */
@ExtendWith(MockitoExtension.class)
class TracedCohereClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private Cohere mockClient;

    @Mock
    private NonStreamedChatResponse mockChatResponse;

    @Mock
    private EmbedResponse mockEmbedResponse;

    @Mock
    private RerankResponse mockRerankResponse;

    private FITracer tracer;
    private TracedCohereClient tracedClient;

    @BeforeEach
    void setup() {
        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());
        tracedClient = new TracedCohereClient(mockClient, tracer);
    }

    // ========== chat Tests ==========

    @Test
    void shouldCreateSpanForChat() {
        // Arrange
        ChatRequest request = ChatRequest.builder()
            .message("Hello!")
            .build();

        when(mockClient.chat(any(ChatRequest.class))).thenReturn(mockChatResponse);
        when(mockChatResponse.getText()).thenReturn("Hi there!");
        when(mockChatResponse.getFinishReason()).thenReturn(Optional.empty());
        when(mockChatResponse.getGenerationId()).thenReturn(Optional.empty());
        when(mockChatResponse.getMeta()).thenReturn(Optional.empty());
        when(mockChatResponse.getToolCalls()).thenReturn(Optional.empty());

        // Act
        NonStreamedChatResponse result = tracedClient.chat(request);

        // Assert
        assertThat(result).isSameAs(mockChatResponse);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Cohere Chat");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.LLM.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("cohere");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldCaptureOutputValueForChat() {
        // Arrange
        String expectedOutput = "This is the response";
        ChatRequest request = ChatRequest.builder()
            .message("Hello!")
            .build();

        when(mockClient.chat(any(ChatRequest.class))).thenReturn(mockChatResponse);
        when(mockChatResponse.getText()).thenReturn(expectedOutput);
        when(mockChatResponse.getFinishReason()).thenReturn(Optional.empty());
        when(mockChatResponse.getGenerationId()).thenReturn(Optional.empty());
        when(mockChatResponse.getMeta()).thenReturn(Optional.empty());
        when(mockChatResponse.getToolCalls()).thenReturn(Optional.empty());

        // Act
        tracedClient.chat(request);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)
        )).isEqualTo(expectedOutput);
    }

    @Test
    void shouldSetModelAttributesForChat() {
        // Arrange
        ChatRequest request = ChatRequest.builder()
            .message("Hello!")
            .model("command-r-plus")
            .build();

        when(mockClient.chat(any(ChatRequest.class))).thenReturn(mockChatResponse);
        when(mockChatResponse.getText()).thenReturn("Response");
        when(mockChatResponse.getFinishReason()).thenReturn(Optional.empty());
        when(mockChatResponse.getGenerationId()).thenReturn(Optional.empty());
        when(mockChatResponse.getMeta()).thenReturn(Optional.empty());
        when(mockChatResponse.getToolCalls()).thenReturn(Optional.empty());

        // Act
        tracedClient.chat(request);

        // Assert
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_MODEL_NAME)
        )).isEqualTo("command-r-plus");
    }

    @Test
    void shouldRecordErrorOnChatFailure() {
        // Arrange
        ChatRequest request = ChatRequest.builder()
            .message("Hello!")
            .build();

        when(mockClient.chat(any(ChatRequest.class)))
            .thenThrow(new RuntimeException("API connection error"));

        // Act & Assert
        // TracedCohereClient wraps exceptions in RuntimeException("Cohere chat failed", e)
        assertThatThrownBy(() -> tracedClient.chat(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Cohere chat failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== embed Tests ==========

    @Test
    void shouldCreateSpanForEmbed() {
        // Arrange
        EmbedRequest request = EmbedRequest.builder()
            .texts(List.of("Hello, world!"))
            .model("embed-english-v3.0")
            .build();

        when(mockClient.embed(any(EmbedRequest.class))).thenReturn(mockEmbedResponse);
        // The embed method calls response.visit() - we need to handle the visitor pattern
        doAnswer(invocation -> {
            EmbedResponse.Visitor<?> visitor = invocation.getArgument(0);
            return null;
        }).when(mockEmbedResponse).visit(any());

        // Act
        EmbedResponse result = tracedClient.embed(request);

        // Assert
        assertThat(result).isSameAs(mockEmbedResponse);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Cohere Embed");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("cohere");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldRecordErrorOnEmbedFailure() {
        // Arrange
        EmbedRequest request = EmbedRequest.builder()
            .texts(List.of("Test text"))
            .build();

        when(mockClient.embed(any(EmbedRequest.class)))
            .thenThrow(new RuntimeException("Embed service unavailable"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.embed(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Cohere embed failed");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    // ========== rerank Tests ==========

    @Test
    void shouldCreateSpanForRerank() {
        // Arrange
        RerankRequest request = RerankRequest.builder()
            .query("What is machine learning?")
            .documents(List.of(
                RerankRequestDocumentsItem.of("ML is a subset of AI"),
                RerankRequestDocumentsItem.of("Deep learning uses neural networks")
            ))
            .model("rerank-english-v3.0")
            .build();

        RerankResponseResultsItem mockResult = mock(RerankResponseResultsItem.class);
        when(mockResult.getRelevanceScore()).thenReturn(0.95f);
        when(mockResult.getIndex()).thenReturn(0);

        when(mockClient.rerank(any(RerankRequest.class))).thenReturn(mockRerankResponse);
        when(mockRerankResponse.getResults()).thenReturn(List.of(mockResult));
        when(mockRerankResponse.getMeta()).thenReturn(Optional.empty());

        // Act
        RerankResponse result = tracedClient.rerank(request);

        // Assert
        assertThat(result).isSameAs(mockRerankResponse);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getName()).isEqualTo("Cohere Rerank");
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.RERANKER.getValue());
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER)
        )).isEqualTo("cohere");
        assertThat(spanData.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void shouldRecordErrorOnRerankFailure() {
        // Arrange
        RerankRequest request = RerankRequest.builder()
            .query("Test query")
            .documents(List.of(RerankRequestDocumentsItem.of("doc1")))
            .build();

        when(mockClient.rerank(any(RerankRequest.class)))
            .thenThrow(new RuntimeException("Rerank service unavailable"));

        // Act & Assert
        assertThatThrownBy(() -> tracedClient.rerank(request))
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("Cohere rerank failed");

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
}
