package ai.traceai.googlegenai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.genai.Client;
import com.google.genai.ResponseStream;
import com.google.genai.types.GenerateContentResponse;
import com.google.genai.types.Content;
import com.google.genai.types.GenerateContentConfig;
import com.google.genai.types.HttpOptions;
import com.google.genai.types.Part;
import com.google.genai.types.Tool;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

/** Real SDK, synthetic credentials and loopback only, including the SDK's tool loop. */
@Execution(ExecutionMode.SAME_THREAD)
public class AsyncContextTest {
    private static final String ANSWER = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"answer\"}]},\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":3,\"candidatesTokenCount\":2,\"thoughtsTokenCount\":5,\"totalTokenCount\":10}}";
    private static final String TOOL = "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"functionCall\":{\"name\":\"lookup\",\"args\":{}}}]},\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":11,\"candidatesTokenCount\":4,\"totalTokenCount\":15}}";
    private static FITracer toolTracer;

    public static String lookup() {
        Span tool = toolTracer.startSpan("lookup-tool", FISpanKind.TOOL);
        tool.end();
        return "synthetic lookup result";
    }

    static final class Fixture implements AutoCloseable {
        final HttpServer server;
        final ExecutorService executor = Executors.newCachedThreadPool();
        final InMemorySpanExporter exporter = InMemorySpanExporter.create();
        final SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        final AtomicInteger requests = new AtomicInteger();
        final CountDownLatch received = new CountDownLatch(1), release = new CountDownLatch(1);
        final Client client;
        final FITracer tracer = new FITracer(provider.get("context-test"), TraceConfig.builder().build());
        final TracedGenerativeModel model;
        boolean streaming;
        boolean error;

        Fixture(boolean blockFirst, boolean tools) throws Exception {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.setExecutor(executor);
            server.createContext("/", exchange -> {
                String request = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                int n = requests.incrementAndGet();
                received.countDown();
                if (blockFirst && n == 1) {
                    try {
                        if (!release.await(10, TimeUnit.SECONDS)) throw new AssertionError("release timeout");
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }
                String body = tools && request.contains("\"tools\"") && !request.contains("\"functionResponse\"")
                    ? TOOL : ANSWER;
                if (streaming) body = "data: " + body + "\n\n";
                if (error) body = "{\"error\":{\"code\":400,\"message\":\"synthetic error\",\"status\":\"INVALID_ARGUMENT\"}}";
                byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", streaming ? "text/event-stream" : "application/json");
                exchange.sendResponseHeaders(error ? 400 : 200, bytes.length);
                try (var out = exchange.getResponseBody()) { out.write(bytes); }
                exchange.close();
            });
            server.start();
            client = Client.builder().apiKey("synthetic-key").vertexAI(false)
                .httpOptions(HttpOptions.builder().baseUrl("http://127.0.0.1:" + server.getAddress().getPort()).build()).build();
            model = new TracedGenerativeModel(client, tracer, "gemini-test");
        }

        GenerateContentConfig tools() throws Exception {
            toolTracer = tracer;
            return GenerateContentConfig.builder().tools(List.of(Tool.builder()
                .functions(List.of(AsyncContextTest.class.getMethod("lookup"))).build())).build();
        }

        SpanData generation() {
            return exporter.getFinishedSpanItems().stream()
                .filter(s -> s.getName().contains("Generate Content")).findFirst().orElseThrow();
        }

        public void close() throws Exception {
            release.countDown();
            client.close();
            server.stop(0);
            executor.shutdownNow();
            provider.close();
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void toolSpanIsChildOfAsyncGeneration(boolean structured) throws Exception {
        try (Fixture f = new Fixture(false, true)) {
            var parent = f.provider.get("test").spanBuilder("caller").startSpan();
            var config = f.tools();
            try (Scope ignored = parent.makeCurrent()) {
                var future = structured
                    ? f.model.generateContentAsync(List.of(Content.builder().role("user").parts(List.of(Part.fromText("question"))).build()), config)
                    : f.model.generateContentAsync("question", config);
                assertThat(Span.current()).isSameAs(parent);
                assertThat(future.get(10, TimeUnit.SECONDS).text()).isEqualTo("answer");
                assertThat(Span.current()).isSameAs(parent);
            }
            assertThat(f.requests.get()).isEqualTo(2);
            assertThat(f.exporter.getFinishedSpanItems()).hasSize(2);
            var tool = f.exporter.getFinishedSpanItems().stream()
                .filter(s -> s.getName().equals("lookup-tool")).findFirst().orElseThrow();
            assertThat(tool.getParentSpanId()).isEqualTo(f.generation().getSpanId());
            assertThat(tool.getTraceId()).isEqualTo(f.generation().getTraceId());
            assertThat(f.generation().getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            parent.end();
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void callbackGenerationIsSiblingNotChild(boolean tools) throws Exception {
        try (Fixture f = new Fixture(true, tools)) {
            var config = tools ? f.tools() : null;
            var parent = f.provider.get("test").spanBuilder("caller").startSpan();
            CompletableFuture<?> chain;
            try (Scope ignored = parent.makeCurrent()) {
                var first = f.model.generateContentAsync("first", config);
                assertThat(f.received.await(5, TimeUnit.SECONDS)).isTrue();
                chain = first.thenApply(response -> {
                    assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
                    return f.model.generateContent("second");
                });
            }
            f.release.countDown();
            chain.get(10, TimeUnit.SECONDS);
            var generations = f.exporter.getFinishedSpanItems().stream()
                .filter(s -> s.getName().contains("Generate Content")).toList();
            assertThat(generations).hasSize(2).allSatisfy(s ->
                assertThat(s.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId()));
            parent.end();
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void streamingCallbackHasCallerContext(boolean error) throws Exception {
        try (Fixture f = new Fixture(true, false)) {
            f.streaming = true;
            f.error = error;
            var parent = f.provider.get("test").spanBuilder("caller").startSpan();
            CompletableFuture<?> chain;
            try (Scope ignored = parent.makeCurrent()) {
                var first = f.model.generateContentStreamAsync("first", null);
                assertThat(f.received.await(5, TimeUnit.SECONDS)).isTrue();
                chain = first.handle((stream, failure) -> {
                    try {
                        assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
                        assertThat(failure != null).isEqualTo(error);
                    } finally {
                        if (stream != null) stream.close();
                    }
                    return null;
                });
            }
            f.release.countDown();
            chain.get(10, TimeUnit.SECONDS);
            assertThat(f.exporter.getFinishedSpanItems()).hasSize(1);
            parent.end();
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void generationAndEmbeddingErrorCallbacksHaveCallerContext(boolean embedding) throws Exception {
        try (Fixture f = new Fixture(true, false)) {
            f.error = true;
            var parent = f.provider.get("test").spanBuilder("caller").startSpan();
            CompletableFuture<?> chain;
            try (Scope ignored = parent.makeCurrent()) {
                var first = embedding ? f.model.embedContentAsync("first", null)
                    : f.model.generateContentAsync("first", null);
                assertThat(f.received.await(5, TimeUnit.SECONDS)).isTrue();
                chain = first.handle((response, failure) -> {
                    assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
                    assertThat(failure).isNotNull();
                    return null;
                });
            }
            f.release.countDown();
            chain.get(10, TimeUnit.SECONDS);
            assertThat(f.exporter.getFinishedSpanItems()).hasSize(1);
            parent.end();
        }
    }

    @Test
    void concurrentToolCallsDoNotShareOperationContext() throws Exception {
        try (Fixture f = new Fixture(false, true)) {
            var config = f.tools();
            var parents = new ArrayList<Span>();
            var futures = new ArrayList<CompletableFuture<?>>();
            for (int i = 0; i < 4; i++) {
                Span parent = f.provider.get("test").spanBuilder("caller-" + i).startSpan();
                parents.add(parent);
                try (Scope ignored = parent.makeCurrent()) {
                    futures.add(f.model.generateContentAsync("question", config));
                }
            }
            CompletableFuture.allOf(futures.toArray(new CompletableFuture<?>[0])).get(10, TimeUnit.SECONDS);
            var spans = f.exporter.getFinishedSpanItems();
            assertThat(f.requests.get()).isEqualTo(8);
            assertThat(spans).hasSize(8);
            var generations = spans.stream().filter(s -> s.getName().contains("Generate Content")).toList();
            assertThat(generations).extracting(SpanData::getParentSpanId).containsExactlyInAnyOrderElementsOf(
                parents.stream().map(p -> p.getSpanContext().getSpanId()).toList());
            var tools = spans.stream().filter(s -> s.getName().equals("lookup-tool")).toList();
            assertThat(tools).extracting(SpanData::getParentSpanId).containsExactlyInAnyOrderElementsOf(
                generations.stream().map(SpanData::getSpanId).toList());
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
            parents.forEach(Span::end);
        }
    }

    @Test
    @SuppressWarnings("unchecked")
    void streamConstructionFailureRestoresCallerAndCompletingThreadContexts() throws Exception {
        try (Fixture f = new Fixture(false, false)) {
            var key = ContextKey.<String>named("caller-value");
            var caller = Context.root().with(key, "caller");
            var worker = Context.root().with(key, "worker");
            ResponseStream<GenerateContentResponse> source = mock(ResponseStream.class,
                withSettings().mockMaker(org.mockito.MockMakers.SUBCLASS));
            var failure = new IllegalStateException("synthetic iterator failure");
            when(source.iterator()).thenThrow(failure);
            var pending = new CompletableFuture<ResponseStream<GenerateContentResponse>>();
            CompletableFuture<?> callback;
            try (Scope ignored = caller.makeCurrent()) {
                var op = new GenAiOperation(f.tracer, "test", "gemini-test", FISpanKind.LLM, "question", null);
                callback = TracedGenerativeModel.asyncStream(op, () -> pending).handle((stream, error) -> {
                    assertThat(Context.current().get(key)).isEqualTo("caller");
                    assertThat(Span.current().getSpanContext().isValid()).isFalse();
                    assertThat(error).isSameAs(failure);
                    return null;
                });
            }
            try (Scope ignored = worker.makeCurrent()) {
                pending.complete(source);
                assertThat(Context.current()).isSameAs(worker);
                assertThat(Span.current().getSpanContext().isValid()).isFalse();
            }
            callback.get(10, TimeUnit.SECONDS);
            verify(source).close();
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void usageIncludesEveryAutomaticCallingRound(boolean async) throws Exception {
        try (Fixture f = new Fixture(false, true)) {
            var config = f.tools();
            if (async) f.model.generateContentAsync("question", config).get(10, TimeUnit.SECONDS);
            else f.model.generateContent("question", config);
            assertThat(f.requests.get()).isEqualTo(2);
            // Keep the whole-call total: the SDK's last-response total of 10 undercounts billing.
            assertThat(f.generation().getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
                .isEqualTo(25L);
        }
    }
}
