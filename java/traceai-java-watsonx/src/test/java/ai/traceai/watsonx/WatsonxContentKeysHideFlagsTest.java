package ai.traceai.watsonx;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.ibm.watsonx.ai.chat.ChatHandler;
import com.ibm.watsonx.ai.chat.ChatRequest;
import com.ibm.watsonx.ai.chat.ChatService;
import com.ibm.watsonx.ai.chat.model.SystemMessage;
import com.ibm.watsonx.ai.chat.model.UserMessage;
import com.ibm.watsonx.ai.embedding.EmbeddingService;
import com.ibm.watsonx.ai.textgeneration.TextGenerationRequest;
import com.ibm.watsonx.ai.textgeneration.TextGenerationService;
import com.sun.net.httpserver.HttpServer;
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
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

/**
 * Offline: the real TracedWatsonxAI over the real IBM services talking to a loopback-only fake HTTP server
 * (no IBM endpoint, no token), one hide flag at a time. Every sentinel the active flag covers must be absent
 * from every exported attribute and event.
 */
class WatsonxContentKeysHideFlagsTest {
    static final String IN_TEXT = "SENTINEL-WATSONX-IN-TEXT";
    static final String IN_STREAM = "SENTINEL-WATSONX-IN-STREAM";
    static final String IN_USER = "SENTINEL-WATSONX-IN-USER";
    static final String IN_SYSTEM = "SENTINEL-WATSONX-IN-SYSTEM";
    static final String IN_EMBED = "SENTINEL-WATSONX-IN-EMBED";
    /** The embedding service echoes its input back in the response. */
    static final String IN_EMBED_ECHO = "SENTINEL-WATSONX-ECHO-OF-INPUT";
    static final String OUT_TEXT = "SENTINEL-WATSONX-OUT-TEXT";
    static final String OUT_CHAT = "SENTINEL-WATSONX-OUT-CHAT";
    static final String OUT_TOOL_ARGS = "SENTINEL-WATSONX-OUT-TOOLARGS";

    static final String TEXT = "{\"model_id\":\"test-model\",\"created_at\":\"2026-01-01T00:00:00Z\",\"results\":["
        + "{\"generated_text\":\"" + OUT_TEXT + "\",\"stop_reason\":\"eos_token\",\"input_token_count\":3,"
        + "\"generated_token_count\":2}]}";
    static final String EMBED = "{\"model_id\":\"test-embedding\",\"created_at\":\"2026-01-01T00:00:00Z\","
        + "\"input_token_count\":3,\"results\":[{\"embedding\":[0.1,0.2],\"input\":\"" + IN_EMBED_ECHO + "\"}]}";
    static final String CHAT = "{\"id\":\"chat-test\",\"model_id\":\"test-model\",\"created\":1,\"choices\":["
        + "{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"" + OUT_CHAT + "\"%s},"
        + "\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";
    static final String TOOL_CALLS = ",\"tool_calls\":[{\"id\":\"c1\",\"type\":\"function\",\"function\":"
        + "{\"name\":\"lookup\",\"arguments\":\"{\\\"q\\\":\\\"" + OUT_TOOL_ARGS + "\\\"}\"}}]";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private HttpServer server;

    @BeforeEach
    void setup() throws Exception {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            String path = exchange.getRequestURI().getPath();
            boolean stream = path.endsWith("_stream");
            String json = path.contains("embeddings") ? EMBED
                : path.contains("chat") ? String.format(CHAT, stream ? "" : TOOL_CALLS) : TEXT;
            if (stream && path.contains("chat")) json = json.replace("\"message\"", "\"delta\"");
            byte[] body = (stream ? "data: " + json + "\n\n" : json).getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", stream ? "text/event-stream" : "application/json");
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

    // Only authentication setup differs between the supported IBM releases. No real token is read.
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

    private void run(String flag) throws Exception {
        TraceConfig.Builder b = TraceConfig.builder().apiKey("offline").secretKey("offline").projectName("offline")
            .hideInputs(false).hideOutputs(false).hideInputMessages(false).hideOutputMessages(false)
            .hideErrorDetails(false);
        switch (flag) {
            case "hideInputs" -> b.hideInputs(true);
            case "hideOutputs" -> b.hideOutputs(true);
            case "hideInputMessages" -> b.hideInputMessages(true);
            case "hideOutputMessages" -> b.hideOutputMessages(true);
            default -> throw new IllegalArgumentException(flag);
        }
        FITracer tracer = new FITracer(provider.get("watsonx-hide-test"), b.build());
        new TracedWatsonxAI(service(TextGenerationService.builder(), TextGenerationService.class), tracer)
            .generateText(TextGenerationRequest.builder().input(IN_TEXT).build());
        Object streamRequest = TextGenerationRequest.builder().input(IN_STREAM).build();
        CompletableFuture<Void> streamed = new TracedWatsonxAI(
            service(TextGenerationService.builder(), TextGenerationService.class), tracer).generateTextStream(streamRequest);
        streamed.get(10, TimeUnit.SECONDS);
        new TracedWatsonxAI(service(ChatService.builder(), ChatService.class), tracer).chat(
            ChatRequest.builder().messages(List.of(SystemMessage.of(IN_SYSTEM), UserMessage.text(IN_USER))).build());
        new TracedWatsonxAI(service(ChatService.builder(), ChatService.class), tracer).chatStream(
            ChatRequest.builder().messages(List.of(UserMessage.text(IN_USER))).build(), mock(ChatHandler.class))
            .get(10, TimeUnit.SECONDS);
        new TracedWatsonxAI(service(EmbeddingService.builder(), EmbeddingService.class), tracer)
            .embedText((Object) List.of(IN_EMBED));
        assertThat(exporter.getFinishedSpanItems()).hasSize(5);
    }

    /** Attribute keys (and event names) whose exported value contains the sentinel. */
    private List<String> leakedKeys(String sentinel) {
        List<String> keys = new ArrayList<>();
        for (SpanData span : exporter.getFinishedSpanItems()) {
            span.getAttributes().forEach((k, v) -> { if (String.valueOf(v).contains(sentinel)) keys.add(k.getKey()); });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(sentinel)) keys.add("event:" + e.getName() + "/" + k.getKey());
            }));
        }
        return keys;
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void inputFlagsKeepPromptContentOutOfEverySpan(String flag) throws Exception {
        run(flag);
        for (String s : List.of(IN_TEXT, IN_STREAM, IN_USER, IN_SYSTEM, IN_EMBED)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        if (flag.equals("hideInputs")) assertThat(leakedKeys(IN_EMBED_ECHO)).as("embedding echo").isEmpty();
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(leakedKeys(OUT_TEXT)).isNotEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) throws Exception {
        run(flag);
        for (String s : List.of(OUT_TEXT, OUT_CHAT, OUT_TOOL_ARGS)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        assertThat(leakedKeys(IN_TEXT)).isNotEmpty();
    }
}
