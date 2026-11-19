package ai.traceai.ollama;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.github.ollama4j.OllamaAPI;
import io.github.ollama4j.models.chat.OllamaChatMessage;
import io.github.ollama4j.models.chat.OllamaChatMessageRole;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Offline: the real TracedOllamaAPI over the real Ollama4j transport talking to a loopback-only fake server
 * (no model, no credentials), one hide flag at a time. Every sentinel the active flag covers must be absent
 * from every exported attribute and event.
 */
class OllamaContentKeysHideFlagsTest {
    static final String IN_PROMPT = "SENTINEL-OLLAMA-IN-PROMPT";
    static final String IN_USER = "SENTINEL-OLLAMA-IN-USER";
    static final String IN_SYSTEM = "SENTINEL-OLLAMA-IN-SYSTEM";
    static final String IN_EMBED = "SENTINEL-OLLAMA-IN-EMBED";
    static final String IN_BATCH_A = "SENTINEL-OLLAMA-IN-BATCHA";
    static final String IN_BATCH_B = "SENTINEL-OLLAMA-IN-BATCHB";
    static final String OUT_GENERATE = "SENTINEL-OLLAMA-OUT-GENERATE";
    static final String OUT_CHAT = "SENTINEL-OLLAMA-OUT-CHAT";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private HttpServer server;
    private OllamaAPI api;

    @BeforeEach
    void setup() throws Exception {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            String path = exchange.getRequestURI().getPath();
            exchange.getRequestBody().readAllBytes();
            String response = switch (path) {
                case "/api/generate" -> "{\"model\":\"test-model\",\"response\":\"" + OUT_GENERATE + "\",\"done\":true}";
                case "/api/chat" -> "{\"model\":\"test-model\",\"message\":{\"role\":\"assistant\",\"content\":\""
                    + OUT_CHAT + "\"},\"done\":true}";
                case "/api/embeddings" -> "{\"embedding\":[0.1,0.2,0.3]}";
                default -> throw new IllegalStateException("Unexpected path: " + path);
            };
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, bytes.length);
            try (var output = exchange.getResponseBody()) {
                output.write(bytes);
            }
        });
        server.start();
        api = new OllamaAPI("http://127.0.0.1:" + server.getAddress().getPort());
        api.setRequestTimeoutSeconds(5);
    }

    @AfterEach
    void teardown() {
        server.stop(0);
        provider.close();
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
        TracedOllamaAPI traced = new TracedOllamaAPI(api, new FITracer(provider.get("ollama-hide-test"), b.build()));
        traced.generate("test-model", IN_PROMPT);
        traced.chat("test-model", List.of(
            new OllamaChatMessage(OllamaChatMessageRole.SYSTEM, IN_SYSTEM),
            new OllamaChatMessage(OllamaChatMessageRole.USER, IN_USER)));
        traced.embed("test-model", IN_EMBED);
        traced.embedBatch("test-model", List.of(IN_BATCH_A, IN_BATCH_B));
        assertThat(exporter.getFinishedSpanItems()).hasSize(4);
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
        for (String s : List.of(IN_PROMPT, IN_USER, IN_SYSTEM, IN_EMBED, IN_BATCH_A, IN_BATCH_B)) {
            List<String> leaked = leakedKeys(s);
            // the raw output attribute is outside this check, which covers the input-side keys
            leaked.remove("fi.raw_output");
            assertThat(leaked).as("%s leaked under %s", s, flag).isEmpty();
        }
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(leakedKeys(OUT_GENERATE)).isNotEmpty();
        assertThat(leakedKeys(OUT_CHAT)).isNotEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) throws Exception {
        run(flag);
        for (String s : List.of(OUT_GENERATE, OUT_CHAT)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        assertThat(leakedKeys(IN_PROMPT)).isNotEmpty();
    }
}
