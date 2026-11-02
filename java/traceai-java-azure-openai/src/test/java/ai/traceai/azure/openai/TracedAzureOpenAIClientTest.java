package ai.traceai.azure.openai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.ai.openai.models.ChatCompletionsOptions;
import com.azure.ai.openai.models.ChatRequestUserMessage;
import com.azure.ai.openai.models.CompletionsOptions;
import com.azure.ai.openai.models.EmbeddingsOptions;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpResponse;
import com.azure.core.http.policy.FixedDelayOptions;
import com.azure.core.http.policy.RetryOptions;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Real Azure SDK serialization and response parsing, with an in-process transport only. */
class TracedAzureOpenAIClientTest {
    private static final String CHAT = """
        {"id":"chat-test","created":1700000000,"model":"model-version",
         "choices":[{"index":0,"message":{"role":"assistant","content":"Paris"},"finish_reason":"stop"}],
         "usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}
        """;
    private static final String COMPLETION = """
        {"id":"completion-test","created":1700000000,
         "choices":[{"index":0,"text":"Paris","finish_reason":"stop"}],
         "usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}
        """;
    private static final String EMBEDDING = """
        {"data":[{"index":0,"embedding":[0.1,0.2]}],"usage":{"prompt_tokens":7,"total_tokens":7}}
        """;
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider provider = SdkTracerProvider.builder()
        .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final FITracer tracer = new FITracer(provider.get("azure-test"));
    private final AtomicInteger requests = new AtomicInteger();

    @AfterEach
    void close() {
        provider.close();
    }

    @Test
    void chatRetainsAzureAttributionParentUsageAndResponse() {
        OpenAIClient sdk = client(CHAT, 200, "application/json");
        TracedAzureOpenAIClient traced = new TracedAzureOpenAIClient(sdk, tracer);
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            var result = traced.getChatCompletions("chat-deployment", chatOptions());
            assertThat(result.getChoices().get(0).getMessage().getContent()).isEqualTo("Paris");
            assertThat(traced.unwrap()).isSameAs(sdk);
            SpanData span = onlySpan("Azure OpenAI Chat Completion", "LLM");
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(text(span, SemanticConventions.LLM_REQUEST_MODEL)).isEqualTo("chat-deployment");
            assertThat(text(span, SemanticConventions.LLM_RESPONSE_MODEL)).isEqualTo("model-version");
            assertThat(text(span, SemanticConventions.LLM_RESPONSE_ID)).isEqualTo("chat-test");
            assertThat(text(span, SemanticConventions.LLM_INPUT_MESSAGES)).contains("user", "capital");
            assertThat(text(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Paris");
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
                .isEqualTo(9L);
        } finally {
            parent.end();
        }
        assertThat(requests.get()).isEqualTo(1);
    }

    @Test
    void embeddingsRetainDimensionsAndTokenUsage() {
        var result = new TracedAzureOpenAIClient(client(EMBEDDING, 200, "application/json"), tracer)
            .getEmbeddings("embedding-deployment", new EmbeddingsOptions(List.of("Hello")));
        assertThat(result.getData().get(0).getEmbedding()).hasSize(2);
        SpanData span = onlySpan("Azure OpenAI Embedding", "EMBEDDING");
        assertThat(text(span, SemanticConventions.EMBEDDING_MODEL_NAME)).isEqualTo("embedding-deployment");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(2L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
            .isEqualTo(7L);
    }

    @Test
    void legacyCompletionsRetainOutputAndUsage() {
        var result = new TracedAzureOpenAIClient(client(COMPLETION, 200, "application/json"), tracer)
            .getCompletions("completion-deployment", new CompletionsOptions(List.of("The capital is")));
        assertThat(result.getChoices().get(0).getText()).isEqualTo("Paris");
        SpanData span = onlySpan("Azure OpenAI Completion", "LLM");
        assertThat(text(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Paris");
        assertThat(text(span, SemanticConventions.LLM_RESPONSE_FINISH_REASON)).isEqualTo("stop");
    }

    @Test
    void supportedCallsRespectStandardContentControls() {
        FITracer privateTracer = new FITracer(provider.get("private-test"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideInputMessages(true).hideOutputMessages(true).build());
        new TracedAzureOpenAIClient(client(CHAT, 200, "application/json"), privateTracer)
            .getChatCompletions("chat-deployment", chatOptions());
        SpanData span = onlySpan("Azure OpenAI Chat Completion", "LLM");
        assertThat(span.getAttributes().asMap().toString()).doesNotContain("capital", "Paris");
        assertThat(text(span, SemanticConventions.RAW_INPUT)).isNull();
        assertThat(text(span, SemanticConventions.RAW_OUTPUT)).isNull();
    }

    @Test
    void errorsEndTheSpanWithoutChangingTheSdkFailure() {
        var traced = new TracedAzureOpenAIClient(client(
            "{\"error\":{\"code\":\"BadRequest\",\"message\":\"synthetic rejection\"}}", 400, "application/json"), tracer);
        assertThatThrownBy(() -> traced.getChatCompletions("chat-deployment", chatOptions()))
            .isInstanceOf(com.azure.core.exception.HttpResponseException.class)
            .hasMessageContaining("synthetic rejection");
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(requests.get()).isEqualTo(1);
    }

    @Test
    void unwrapIsAnUninstrumentedEscapeHatchIncludingStreaming() {
        String event = """
            {"id":"chat-stream","created":1700000000,"model":"model-version",
             "choices":[{"index":0,"delta":{"role":"assistant","content":"Paris"},"finish_reason":"stop"}]}
            """.replace("\n", "");
        var traced = new TracedAzureOpenAIClient(client(
            "data: " + event + "\n\ndata: [DONE]\n\n", 200, "text/event-stream"), tracer);
        var chunks = traced.unwrap().getChatCompletionsStream("chat-deployment", chatOptions())
            .stream().toList();
        assertThat(chunks).hasSize(1);
        assertThat(chunks.get(0).getChoices().get(0).getDelta().getContent()).isEqualTo("Paris");
        assertThat(requests.get()).isEqualTo(1);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
    }

    private ChatCompletionsOptions chatOptions() {
        return new ChatCompletionsOptions(List.of(new ChatRequestUserMessage("Name the capital of France")));
    }

    private SpanData onlySpan(String name, String kind) {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getName()).isEqualTo(name);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(text(span, SemanticConventions.LLM_PROVIDER)).isEqualTo("azure");
        // LLM_SYSTEM is a compatibility alias for LLM_PROVIDER in the shared conventions.
        assertThat(text(span, SemanticConventions.LLM_SYSTEM)).isEqualTo("azure");
        assertThat(text(span, SemanticConventions.FI_SPAN_KIND)).isEqualTo(kind);
        return span;
    }

    private String text(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }

    private OpenAIClient client(String body, int status, String contentType) {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        return new OpenAIClientBuilder()
            .endpoint("https://synthetic.openai.azure.com")
            .credential(new AzureKeyCredential("synthetic-not-a-secret"))
            .retryOptions(new RetryOptions(new FixedDelayOptions(0, Duration.ofMillis(1))))
            .httpClient(request -> {
                requests.incrementAndGet();
                return Mono.just(new HttpResponse(request) {
                    private final HttpHeaders headers = new HttpHeaders().set("Content-Type", contentType);
                    @Override public int getStatusCode() { return status; }
                    @Override public String getHeaderValue(String name) { return headers.getValue(name); }
                    @Override public HttpHeaders getHeaders() { return headers; }
                    @Override public Flux<ByteBuffer> getBody() { return Flux.just(ByteBuffer.wrap(bytes)); }
                    @Override public Mono<byte[]> getBodyAsByteArray() { return Mono.just(bytes); }
                    @Override public Mono<String> getBodyAsString() { return Mono.just(body); }
                    @Override public Mono<String> getBodyAsString(Charset charset) {
                        return Mono.just(new String(bytes, charset));
                    }
                });
            }).buildClient();
    }
}
