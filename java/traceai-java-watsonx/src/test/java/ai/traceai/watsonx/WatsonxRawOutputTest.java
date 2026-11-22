package ai.traceai.watsonx;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.ibm.watsonx.ai.embedding.EmbeddingService;
import com.ibm.watsonx.ai.textgeneration.TextGenerationRequest;
import com.ibm.watsonx.ai.textgeneration.TextGenerationService;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CompletableFuture;

import static org.assertj.core.api.Assertions.assertThat;

/** Real released IBM services against a loopback-only fake HTTP transport, never IBM APIs. */
class WatsonxRawOutputTest {
    private static final String PROMPT_SENTINEL = "sentinel-request-prompt-7f3a";
    private static final String TEXT = """
        {"model_id":"test-model","created_at":"2026-01-01T00:00:00Z","results":[
        {"generated_text":"model-answer","stop_reason":"eos_token","input_token_count":3,"generated_token_count":2,
        "input_tokens":[{"text":"%s"}]}]}
        """.formatted(PROMPT_SENTINEL);
    private static final String EMBED = """
        {"model_id":"test-embedding","created_at":"2026-01-01T00:00:00Z","input_token_count":3,
        "results":[{"embedding":[0.1,0.2],"input":"%s"}]}
        """.formatted(PROMPT_SENTINEL);

    private HttpServer server;
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;

    @BeforeEach
    void setup() throws Exception {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            String json = exchange.getRequestURI().getPath().contains("embeddings") ? EMBED : TEXT;
            byte[] body = json.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
    }

    @AfterEach
    void cleanup() {
        server.stop(0);
        provider.close();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void rawOutputOmitsRequestTextUnderInputSettings(String flag) throws Exception {
        // Control: without a flag the raw output is still recorded.
        run(TraceConfig.builder());
        for (SpanData span : exporter.getFinishedSpanItems()) {
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_OUTPUT)))
                .contains(PROMPT_SENTINEL);
        }
        exporter.reset();

        TraceConfig.Builder builder = TraceConfig.builder();
        if (flag.equals("hideInputs")) builder.hideInputs(true); else builder.hideInputMessages(true);
        run(builder);

        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        for (SpanData span : exporter.getFinishedSpanItems()) {
            assertThat(span.getAttributes().toString()).doesNotContain(PROMPT_SENTINEL);
            span.getEvents().forEach(e -> assertThat(e.getAttributes().toString()).doesNotContain(PROMPT_SENTINEL));
        }
    }

    private void run(TraceConfig.Builder builder) throws Exception {
        FITracer tracer = new FITracer(provider.get("raw-output-test"), builder.build());
        var generation = service(TextGenerationService.builder(), TextGenerationService.class);
        var embedding = service(EmbeddingService.builder(), EmbeddingService.class);
        new TracedWatsonxAI(generation, tracer).generateText(TextGenerationRequest.builder().input("question").build());
        new TracedWatsonxAI(embedding, tracer).embedText(List.of("question"));
    }

    // Only authentication setup changed between the tested IBM releases. No real token is read.
    private <T> T service(Object builder, Class<T> type) throws Exception {
        builder.getClass().getMethod("baseUrl", String.class).invoke(builder, "http://127.0.0.1:" + server.getAddress().getPort());
        builder.getClass().getMethod("projectId", String.class).invoke(builder, "offline-project");
        builder.getClass().getMethod("modelId", String.class).invoke(builder, "test-model");
        Class<?> auth;
        String setter;
        try {
            auth = Class.forName("com.ibm.watsonx.ai.core.auth.Authenticator");
            setter = "authenticator";
        } catch (ClassNotFoundException oldSdk) {
            auth = Class.forName("com.ibm.watsonx.ai.core.auth.AuthenticationProvider");
            setter = "authenticationProvider";
        }
        Object stub = Proxy.newProxyInstance(auth.getClassLoader(), new Class<?>[]{auth}, (proxy, method, args) -> {
            if (method.getReturnType() == CompletableFuture.class) return CompletableFuture.completedFuture("offline-token");
            return "offline-token";
        });
        builder.getClass().getMethod(setter, auth).invoke(builder, stub);
        return type.cast(builder.getClass().getMethod("build").invoke(builder));
    }
}
