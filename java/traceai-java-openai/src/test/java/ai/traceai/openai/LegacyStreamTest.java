package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClientImpl;
import com.openai.core.ClientOptions;
import com.openai.core.RequestOptions;
import com.openai.core.http.*;
import com.openai.models.ChatCompletionCreateParams;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.Test;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import static org.assertj.core.api.Assertions.assertThat;

class LegacyStreamTest {
    private static final String CHUNK = "data: {\"id\":\"chat-test\",\"created\":1,\"model\":\"test-model\","
        + "\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":null}]}\n\n";

    @Test void explicitCloseEndsAnUnconsumedStreamAndKeepsTheOldDescriptor() throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var raw = new TracingHttpClientTest.FakeResponse("text/event-stream", CHUNK);
            var client = new OpenAIClientImpl(ClientOptions.builder()
                .httpClient(new TracingHttpClientTest.FakeTransport(raw)).apiKey("offline-not-a-key").maxRetries(0).build());
            var traced = new TracedOpenAIClient(client, new FITracer(provider.get("test")));
            var params = ChatCompletionCreateParams.builder().model("model").messages(List.of()).build();
            var response = traced.streamChatCompletionResponse(params);
            assertThat(raw.reads.get()).isZero();
            response.close();
            response.close();
            assertThat(raw.closes.get()).isEqualTo(1);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(TracedOpenAIClient.class.getMethod("streamChatCompletion", ChatCompletionCreateParams.class).getReturnType())
                .isEqualTo(Iterable.class);
        }
    }

    @Test void shortCircuitAndDoubleInstrumentationKeepOneSpan() throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracer = new FITracer(provider.get("test"));
            var raw = new TracingHttpClientTest.FakeResponse("text/event-stream", CHUNK.repeat(2) + "data: [DONE]\n\n");
            var client = new OpenAIClientImpl(ClientOptions.builder()
                .httpClient(TracingHttpClient.wrap(new TracingHttpClientTest.FakeTransport(raw), tracer))
                .apiKey("offline-not-a-key").maxRetries(0).build());
            var params = ChatCompletionCreateParams.builder().model("model").messages(List.of()).build();
            try (var response = new TracedOpenAIClient(client, tracer).streamChatCompletionResponse(params)) {
                assertThat(response.stream().findFirst().orElseThrow().choices().get(0).delta().content()).contains("hello");
                assertThat(exporter.getFinishedSpanItems()).isEmpty();
            }
            assertThat(raw.closes.get()).isEqualTo(1);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        }
    }

    @Test void readFailureFinishesTheLegacySpanAndClosesTheResponse() throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var raw = new TracingHttpClientTest.FakeResponse("text/event-stream", "");
            raw.body = new InputStream() { public int read() throws java.io.IOException { throw new java.io.IOException("broken"); } };
            var client = new OpenAIClientImpl(ClientOptions.builder()
                .httpClient(new TracingHttpClientTest.FakeTransport(raw)).apiKey("offline-not-a-key").maxRetries(0).build());
            var params = ChatCompletionCreateParams.builder().model("model").messages(List.of()).build();
            try (var response = new TracedOpenAIClient(client, new FITracer(provider.get("test"))).streamChatCompletionResponse(params)) {
                org.assertj.core.api.Assertions.assertThatThrownBy(() -> response.stream().findFirst()).isInstanceOf(java.io.IOException.class);
            }
            assertThat(raw.closes.get()).isGreaterThanOrEqualTo(1);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode())
                .isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
        }
    }

    @Test void checkedFailureBeforeHeadersEndsTheLegacySpan() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var failure = new java.io.IOException("connection-failed");
            var fake = new TracingHttpClientTest.FakeTransport(null) {
                @Override public HttpResponse execute(HttpRequest r, RequestOptions o) { throw OpenAITrace.propagate(failure); }
            };
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(fake).apiKey("offline-not-a-key").maxRetries(0).build());
            var traced = new TracedOpenAIClient(client, new FITracer(provider.get("test")));
            var params = ChatCompletionCreateParams.builder().model("model").messages(List.of()).build();
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> traced.streamChatCompletionResponse(params)).isSameAs(failure);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode())
                .isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
        }
    }

    @Test
    void returningTheStreamMustNotConsumeOrCloseIt() throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracer = new FITracer(provider.get("test"), TraceConfig.builder().build());
            var reads = new AtomicInteger();
            var closes = new AtomicInteger();
            String sse = "data: {\"id\":\"chat-test\",\"created\":1,\"model\":\"test-model\","
                + "\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,"
                + "\"delta\":{\"content\":\"hello\"},\"finish_reason\":null}]}\n\ndata: [DONE]\n\n";
            InputStream body = new ByteArrayInputStream(sse.getBytes(StandardCharsets.UTF_8)) {
                @Override public synchronized int read(byte[] b, int off, int len) {
                    reads.incrementAndGet();
                    return super.read(b, off, len);
                }
            };
            HttpResponse response = new HttpResponse() {
                public int statusCode() { return 200; }
                public Headers headers() { return Headers.builder().put("content-type", "text/event-stream").build(); }
                public InputStream body() { return body; }
                public void close() { closes.incrementAndGet(); }
            };
            HttpClient transport = new HttpClient() {
                public HttpResponse execute(HttpRequest r, RequestOptions o) { return response; }
                public HttpResponse execute(HttpRequest r) { return response; }
                public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) {
                    return CompletableFuture.completedFuture(response);
                }
                public CompletableFuture<HttpResponse> executeAsync(HttpRequest r) {
                    return CompletableFuture.completedFuture(response);
                }
                public void close() {}
            };
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(transport)
                .apiKey("offline-not-a-key").maxRetries(0).build());
            var params = ChatCompletionCreateParams.builder().model("test-model")
                .addMessage(com.openai.models.ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                    com.openai.models.ChatCompletionUserMessageParam.builder()
                        .content(com.openai.models.ChatCompletionUserMessageParam.Content.ofTextContent("legacy-question")).build()))
                .build();
            var chunks = new TracedOpenAIClient(client, tracer).streamChatCompletion(params);
            assertThat(reads.get()).isZero();
            assertThat(closes.get()).isZero();
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            assertThat(chunks).hasSize(1);
            assertThat(closes.get()).isEqualTo(1);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(TracingHttpClientTest.attr(exporter.getFinishedSpanItems().get(0), "gen_ai.input.messages"))
                .contains("legacy-question");
        }
    }
}
