package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClient;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.openai.core.RequestOptions;
import com.openai.core.http.HttpRequest;
import com.openai.core.http.HttpResponse;
import com.openai.models.chat.completions.ChatCompletionCreateParams;
import com.openai.models.embeddings.EmbeddingCreateParams;
import com.openai.models.responses.ResponseCreateParams;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import static ai.traceai.openai.TracingHttpClientTest.*;
import static org.assertj.core.api.Assertions.*;

/** Loads the packaged adapter binary, not recompiled legacy model imports, with OpenAI Java 4.x. */
class ModernOpenAIClientTest {
    private static final String CHAT = "{\"id\":\"c1\",\"object\":\"chat.completion\",\"created\":1,\"model\":\"model\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"hello\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3,\"total_tokens\":5}}";
    private static final String RESPONSE = "{\"id\":\"r1\",\"object\":\"response\",\"created_at\":1,\"model\":\"model\",\"status\":\"completed\",\"output\":[{\"type\":\"message\",\"id\":\"m1\",\"role\":\"assistant\",\"status\":\"completed\",\"content\":[{\"type\":\"output_text\",\"text\":\"hello\",\"annotations\":[]}]}],\"usage\":{\"input_tokens\":2,\"output_tokens\":3,\"total_tokens\":5}}";
    private static final String CHUNK = "data: {\"id\":\"c1\",\"created\":1,\"model\":\"model\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":null}]}\n\n";
    final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    final SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    final FITracer tracer = new FITracer(provider.get("modern"), TraceConfig.builder().build());
    @AfterEach void closeProvider() { provider.close(); }

    OpenAIClient client(FakeTransport transport) {
        return new OpenAIClientImpl(ClientOptions.builder().httpClient(TracingHttpClient.wrap(transport, tracer))
            .apiKey("offline-not-a-key").maxRetries(0).build());
    }
    ChatCompletionCreateParams chat() { return ChatCompletionCreateParams.builder().model("model").addUserMessage("hello").build(); }
    ResponseCreateParams response() { return ResponseCreateParams.builder().model("model").input("hello").build(); }

    @Test void nativeAsyncRetainsTheCallingSpanAcrossTheSdkExecutor() throws Exception {
        var transport = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("application/json", RESPONSE)), tracer);
        var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(transport)
            .apiKey("offline-not-a-key").maxRetries(0).build());
        var parent = provider.get("modern").spanBuilder("parent").startSpan();
        try (AutoCloseable close = client::close; var scope = parent.makeCurrent()) {
            var requestClient = client.withOptions(options -> options.httpClient(TracingHttpClient.inCurrentContext(transport)));
            requestClient.async().responses().create(response()).get(5, TimeUnit.SECONDS);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(exporter.getFinishedSpanItems().get(0).getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        } finally { parent.end(); }
    }

    @Test void nativeChatSyncAndAsyncPreserveTypedResults() throws Exception {
        for (boolean async : new boolean[] {false, true}) {
            var raw = new FakeResponse("application/json", CHAT);
            var fake = new FakeTransport(raw);
            var client = client(fake);
            try (AutoCloseable close = client::close) {
                var result = async ? client.async().chat().completions().create(chat()).get(5, TimeUnit.SECONDS)
                    : client.chat().completions().create(chat());
                assertThat(result.choices().get(0).message().content()).contains("hello");
                assertThat(raw.closes.get()).isEqualTo(1);
                assertThat(fake.requestBytes).contains("hello", "model");
            }
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        assertThat(attr(exporter.getFinishedSpanItems().get(0), "gen_ai.request.model")).isEqualTo("model");
    }

    @Test void nativeResponsesSyncAndAsyncRemainAvailable() throws Exception {
        for (boolean async : new boolean[] {false, true}) {
            var raw = new FakeResponse("application/json", RESPONSE);
            var client = client(new FakeTransport(raw));
            try (AutoCloseable close = client::close) {
                var result = async ? client.async().responses().create(response()).get(5, TimeUnit.SECONDS)
                    : client.responses().create(response());
                assertThat(result.id()).isEqualTo("r1");
                assertThat(result.output().get(0).message().orElseThrow().content().get(0).outputText().orElseThrow().text()).isEqualTo("hello");
                assertThat(raw.closes.get()).isEqualTo(1);
            }
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        assertThat(attr(exporter.getFinishedSpanItems().get(0), "gen_ai.operation.name")).isEqualTo("responses");
    }

    @Test void nativeEmbeddingsRemainAvailable() throws Exception {
        var raw = new FakeResponse("application/json", "{\"object\":\"list\",\"model\":\"embed\",\"data\":[{\"object\":\"embedding\",\"index\":0,\"embedding\":[0.1,0.2]}],\"usage\":{\"prompt_tokens\":2,\"total_tokens\":2}}");
        var client = client(new FakeTransport(raw));
            try (AutoCloseable close = client::close) {
            var result = client.embeddings().create(EmbeddingCreateParams.builder().model("embed").input("hello").build());
            assertThat(result.data().get(0).embedding()).hasSize(2);
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(attr(exporter.getFinishedSpanItems().get(0), "gen_ai.operation.name")).isEqualTo("embeddings");
    }

    @Test void nativeChatStreamingRemainsLazyAndCloseable() throws Exception {
        var raw = new FakeResponse("text/event-stream", CHUNK + "data: [DONE]\n\n");
        var client = client(new FakeTransport(raw));
        try (AutoCloseable close = client::close; var response = client.chat().completions().createStreaming(chat())) {
            assertThat(raw.reads.get()).isZero();
            assertThat(raw.closes.get()).isZero();
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            assertThat(response.stream().findFirst().orElseThrow().choices().get(0).delta().content()).contains("hello");
        }
        assertThat(raw.closes.get()).isEqualTo(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test void nativeResponsesStreamingAndAsyncStreamingAreNotReplaced() throws Exception {
        String sse = "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\",\"item_id\":\"m1\",\"output_index\":0,\"content_index\":0,\"sequence_number\":1}\n\n"
            + "event: response.completed\ndata: {\"type\":\"response.completed\",\"sequence_number\":2,\"response\":" + RESPONSE + "}\n\n";
        for (boolean async : new boolean[] {false, true}) {
            var raw = new FakeResponse("text/event-stream", sse);
            var client = client(new FakeTransport(raw));
            try (AutoCloseable close = client::close) {
                if (async) {
                    var stream = client.async().responses().createStreaming(response());
                    try (AutoCloseable closeStream = stream::close) {
                        var count = new java.util.concurrent.atomic.AtomicInteger();
                        stream.subscribe(event -> count.incrementAndGet()).onCompleteFuture().get(5, TimeUnit.SECONDS);
                        assertThat(count.get()).isEqualTo(2);
                    }
                } else {
                    try (var stream = client.responses().createStreaming(response())) {
                        assertThat(stream.stream().count()).isEqualTo(2);
                    }
                }
            }
            assertThat(raw.closes.get()).isEqualTo(1);
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @Test void nativeAsyncCancellationMatchesTheUnwrappedSdk() throws Exception {
        // The generated service currently cancels its result, not the transport future. Tracing
        // preserves that SDK contract; direct HttpClient cancellation is tested separately.
        for (boolean traced : new boolean[] {false, true}) {
            var pending = new CompletableFuture<HttpResponse>();
            var fake = new FakeTransport(null) {
                @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) { return pending; }
            };
            var client = new OpenAIClientImpl(ClientOptions.builder()
                .httpClient(traced ? TracingHttpClient.wrap(fake, tracer) : fake)
                .streamHandlerExecutor(Runnable::run).apiKey("offline-not-a-key").maxRetries(0).build());
            var raw = new FakeResponse("application/json", RESPONSE);
            try (AutoCloseable close = client::close) {
                var result = client.async().responses().create(response());
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
                while (pending.getNumberOfDependents() == 0 && System.nanoTime() < deadline) Thread.onSpinWait();
                assertThat(pending.getNumberOfDependents()).isPositive();
                assertThat(result.cancel(true)).isTrue();
                assertThat(result.isCancelled()).isTrue();
                assertThat(pending.isCancelled()).isFalse();
                pending.complete(raw);
                assertThat(raw.closes.get()).isZero();
            }
            // The wrapper additionally releases abandoned responses at client shutdown.
            assertThat(raw.closes.get()).isEqualTo(traced ? 1 : 0);
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test void realHttpFirstTokenArrivesBeforeTheServerFinishes() throws Exception {
        var release = new CountDownLatch(1);
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        var threads = Executors.newCachedThreadPool();
        server.setExecutor(threads);
        server.createContext("/v1/chat/completions", exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try (var output = exchange.getResponseBody()) {
                output.write(CHUNK.getBytes(StandardCharsets.UTF_8));
                output.flush();
                try { release.await(10, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                output.write("data: [DONE]\n\n".getBytes(StandardCharsets.UTF_8));
            } finally { exchange.close(); }
        });
        server.start();
        var client = new OpenAIClientImpl(ClientOptions.builder()
                .httpClient(TracingHttpClient.wrap(OkHttpClient.builder().build(), tracer))
                .apiKey("offline-not-a-key").baseUrl("http://127.0.0.1:" + server.getAddress().getPort() + "/v1")
                .maxRetries(0).build());
        try (AutoCloseable close = client::close) {
            var first = threads.submit(() -> {
                try (var response = client.chat().completions().createStreaming(chat())) {
                    return response.stream().findFirst().orElseThrow().choices().get(0).delta().content().orElseThrow();
                }
            });
            assertThat(first.get(5, TimeUnit.SECONDS)).isEqualTo("hello");
            assertThat(release.getCount()).isEqualTo(1);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        } finally {
            release.countDown();
            server.stop(0);
            threads.shutdownNow();
        }
    }
}
