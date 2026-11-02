package ai.traceai.spring.compatibility;

import ai.traceai.FITracer;
import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.TracedChatModel;
import ai.traceai.spring.TracedEmbeddingModel;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.observation.ObservationRegistry;
import io.micrometer.tracing.handler.DefaultTracingObservationHandler;
import io.micrometer.tracing.otel.bridge.OtelCurrentTraceContext;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.tool.function.FunctionToolCallback;
import static org.junit.jupiter.api.Assertions.*;

@Timeout(30)
class NativeSpringAITest {
    static final String INPUT = "private-prompt-7ef2";
    static final String OUTPUT = "private-answer-c1a4";
    static final String CHAT = """
        {"id":"synthetic-id","created":1,"object":"chat.completion","model":"synthetic-model","choices":[{"index":0,"message":{"role":"assistant","content":"private-answer-c1a4"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}
        """;
    static final String EMBEDDING = """
        {"object":"list","model":"synthetic-embedding","data":[{"object":"embedding","index":0,"embedding":[0.1,0.2]}],"usage":{"prompt_tokens":3,"total_tokens":3}}
        """;
    static final String STREAM = """
        data: {"id":"synthetic-id","created":1,"object":"chat.completion.chunk","model":"synthetic-model","choices":[{"index":0,"delta":{"role":"assistant","content":"private-answer-c1a4"},"finish_reason":null}]}

        data: {"id":"synthetic-id","created":1,"object":"chat.completion.chunk","model":"synthetic-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

        data: {"id":"synthetic-id","created":1,"object":"chat.completion.chunk","model":"synthetic-model","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}

        data: [DONE]

        """;

    @Test void metadataOnlyByDefaultAndOneNativeSpanUnderApplicationParent() throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter())) {
            f.reply(CHAT);
            var parent = f.sdk.get("application").spanBuilder("parent").startSpan();
            try (var ignored = parent.makeCurrent()) {
                assertEquals(OUTPUT, Models.chat(f.url(), f.registry).call(new Prompt(INPUT)).getResult().getOutput().getText());
            } finally { parent.end(); }
            assertEquals(1, f.aiSpans().size(), "one model span; native HTTP spans may also be present");
            var span = f.spans().stream().filter(s -> "LLM".equals(attr(s, "fi.span.kind"))).findFirst().orElseThrow();
            assertEquals(parent.getSpanContext().getSpanId(), span.getParentSpanId());
            assertEquals("openai", attr(span, "gen_ai.provider.name"));
            assertEquals("synthetic-model", attr(span, "gen_ai.response.model"));
            assertEquals("3", attr(span, "gen_ai.usage.input_tokens"));
            assertEquals("2", attr(span, "gen_ai.usage.output_tokens"));
            assertFalse(span.getAttributes().toString().contains(INPUT));
            assertFalse(span.getAttributes().toString().contains(OUTPUT));
            assertEquals("spring-ai-test", attrResource(span, "project_name"));
        }
    }

    @ParameterizedTest @CsvSource({"false,false", "true,false", "false,true", "true,true"})
    void contentIsExplicitAndIndependent(boolean inputs, boolean outputs) throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter(inputs, outputs))) {
            f.reply(CHAT);
            Models.chat(f.url(), f.registry).call(new Prompt(INPUT));
            var span = f.onlySpan();
            assertEquals(inputs ? INPUT : null, attr(span, "input.value"));
            assertEquals(outputs ? OUTPUT : null, attr(span, "output.value"));
        }
    }

    @Test void nativeStreamAggregatesOnceAndCancellationEndsSpan() throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter(true, true))) {
            f.replies.add(new Reply(STREAM, "text/event-stream", 200));
            var model = Models.chat(f.url(), f.registry);
            model.stream(new Prompt(INPUT)).blockLast(Duration.ofSeconds(10));
            var span = f.onlySpan();
            assertEquals(OUTPUT, attr(span, "output.value"));
            assertEquals("2", attr(span, "gen_ai.usage.output_tokens"));
            f.exporter.reset();
            f.replies.add(new Reply(STREAM, "text/event-stream", 200));
            model.stream(new Prompt(INPUT)).take(1).blockLast(Duration.ofSeconds(10));
            assertEquals("LLM", attr(f.onlySpan(), "fi.span.kind"));
        }
    }

    @Test void embeddingConveniencePathUsesNativeObservation() throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter(true, false))) {
            f.reply(EMBEDDING);
            assertEquals(2, Models.embedding(f.url(), f.registry).embed(List.of(INPUT)).get(0).length);
            var span = f.onlySpan();
            assertEquals("EMBEDDING", attr(span, "fi.span.kind"));
            assertEquals(INPUT, attr(span, "input.value"));
            assertFalse(span.getAttributes().toString().contains("0.1"));
        }
    }

    public record ToolInput(String city) {}

    @Test void realToolExecutionKeepsNativeParentageWithoutExtraSpans() throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter(true, true))) {
            f.reply("""
                {"id":"tool-id","created":1,"object":"chat.completion","model":"synthetic-model","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call1","type":"function","function":{"name":"weather","arguments":"{\\"city\\":\\"synthetic-city\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}
                """);
            f.reply(CHAT);
            var callback = FunctionToolCallback.builder("weather", (ToolInput input) -> "synthetic-result")
                .description("Synthetic local tool").inputType(ToolInput.class).build();
            var options = OpenAiChatOptions.builder().toolCallbacks(callback).build();
            var parent = f.sdk.get("application").spanBuilder("tool-workflow").startSpan();
            try (var ignored = parent.makeCurrent()) {
                assertEquals(OUTPUT, Models.toolChat(f.url(), f.registry, new Prompt(INPUT, options)).getResult().getOutput().getText());
            } finally { parent.end(); }
            assertEquals(3, f.aiSpans().size(), "two model requests and one tool, no wrapper spans");
            var tool = f.spans().stream().filter(s -> "TOOL".equals(attr(s, "fi.span.kind"))).findFirst().orElseThrow();
            assertEquals("weather", attr(tool, "gen_ai.tool.name"));
            assertTrue(attr(tool, "input.value").contains("synthetic-city"));
            assertTrue(attr(tool, "output.value").contains("synthetic-result"));
            assertTrue(f.spans().stream().anyMatch(s -> s.getSpanId().equals(tool.getParentSpanId())));
            assertEquals(1, f.spans().stream().map(SpanData::getTraceId).distinct().count());
        }
    }

    @Test void nativeFailureIsNotSwallowed() throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter())) {
            f.replies.add(new Reply("{\"error\":{\"message\":\"synthetic failure\",\"type\":\"invalid_request_error\"}}", "application/json", 400));
            assertThrows(RuntimeException.class, () -> Models.chat(f.url(), f.registry).call(new Prompt(INPUT)));
            assertEquals(io.opentelemetry.api.trace.StatusCode.ERROR, f.onlySpan().getStatus().getStatusCode());
        }
    }

    @Test void legacyPublicEntryPointsWorkWithoutNativeTracing() throws Exception {
        try (Fixture f = new Fixture(new SpringAIObservationFilter())) {
            var tracer = new FITracer(f.sdk.get("legacy"));
            f.reply(CHAT);
            var chat = new TracedChatModel(Models.chat(f.url(), ObservationRegistry.NOOP), tracer, "openai");
            assertEquals(OUTPUT, chat.call(new Prompt(INPUT)).getResult().getOutput().getText());
            assertEquals(1, f.spans().size());
            f.exporter.reset();
            f.reply(EMBEDDING);
            var embedding = new TracedEmbeddingModel(Models.embedding(f.url(), ObservationRegistry.NOOP), tracer, "openai");
            assertEquals(2, embedding.embed(INPUT).length);
            assertEquals(1, f.spans().size());
            f.exporter.reset();
            f.replies.add(new Reply(STREAM, "text/event-stream", 200));
            chat.stream(new Prompt(INPUT)).blockLast(Duration.ofSeconds(10));
            var stream = f.onlySpan();
            assertEquals(OUTPUT, attr(stream, "output.value"));
            assertEquals(3L, stream.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens")));
            assertEquals(2L, stream.getAttributes().get(AttributeKey.longKey("gen_ai.usage.output_tokens")));
        }
    }

    static String attr(SpanData span, String name) { return span.getAttributes().get(AttributeKey.stringKey(name)); }
    static String attrResource(SpanData span, String name) { return span.getResource().getAttribute(AttributeKey.stringKey(name)); }
    record Reply(String body, String contentType, int status) {}

    static final class Fixture implements AutoCloseable {
        final HttpServer server;
        final ConcurrentLinkedQueue<Reply> replies = new ConcurrentLinkedQueue<>();
        final InMemorySpanExporter exporter = InMemorySpanExporter.create();
        final SdkTracerProvider sdk;
        final ObservationRegistry registry = ObservationRegistry.create();
        Fixture(SpringAIObservationFilter filter) throws Exception {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/", exchange -> {
                exchange.getRequestBody().readAllBytes();
                Reply reply = replies.poll();
                if (reply == null) reply = new Reply("{}", "application/json", 500);
                byte[] bytes = reply.body().getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().add("Content-Type", reply.contentType());
                exchange.sendResponseHeaders(reply.status(), bytes.length);
                exchange.getResponseBody().write(bytes);
                exchange.close();
            });
            server.start();
            sdk = SdkTracerProvider.builder()
                .setResource(Resource.create(Attributes.of(AttributeKey.stringKey("project_name"), "spring-ai-test")))
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
            var tracer = new OtelTracer(sdk.get("spring-native"), new OtelCurrentTraceContext(), event -> {});
            registry.observationConfig().observationHandler(new DefaultTracingObservationHandler(tracer)).observationFilter(filter);
        }
        String url() { return "http://127.0.0.1:" + server.getAddress().getPort(); }
        void reply(String body) { replies.add(new Reply(body, "application/json", 200)); }
        List<SpanData> spans() { return exporter.getFinishedSpanItems(); }
        List<SpanData> aiSpans() { return spans().stream().filter(s -> attr(s, "fi.span.kind") != null).toList(); }
        SpanData onlySpan() throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
            while (aiSpans().isEmpty() && System.nanoTime() < deadline) Thread.sleep(10);
            assertEquals(1, aiSpans().size());
            return aiSpans().get(0);
        }
        @Override public void close() { sdk.close(); server.stop(0); }
    }
}
