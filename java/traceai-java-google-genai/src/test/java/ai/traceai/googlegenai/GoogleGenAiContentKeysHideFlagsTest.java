package ai.traceai.googlegenai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.genai.Client;
import com.google.genai.types.Content;
import com.google.genai.types.HttpOptions;
import com.google.genai.types.Part;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Offline: the real TracedGenerativeModel over a stubbed Google GenAI client (no network), one hide flag at a
 * time. Every sentinel the active flag covers must be absent from every exported attribute and event.
 */
class GoogleGenAiContentKeysHideFlagsTest {
    static final String IN_PROMPT = "SENTINEL-GENAI-IN-PROMPT";
    static final String IN_CONTENT = "SENTINEL-GENAI-IN-CONTENT";
    static final String IN_COUNT = "SENTINEL-GENAI-IN-COUNT";
    static final String IN_CHAT = "SENTINEL-GENAI-IN-CHAT";
    static final String OUT_TEXT = "SENTINEL-GENAI-OUT-TEXT";
    static final String OUT_CHAT = "SENTINEL-GENAI-OUT-CHAT";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private Client client;
    private HttpServer server;

    private static String responseJson(String text) {
        return "{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":"
            + "[{\"text\":\"" + text + "\"}]},\"finishReason\":\"STOP\"}],"
            + "\"usageMetadata\":{\"promptTokenCount\":3,\"candidatesTokenCount\":5,\"totalTokenCount\":8}}";
    }

    @BeforeEach
    void setup() throws Exception {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        // real client over a canned loopback transport (no external network)
        server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        server.createContext("/", exchange -> {
            String request = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            String path = exchange.getRequestURI().getPath();
            String body;
            if (path.endsWith(":countTokens")) {
                body = "{\"totalTokens\":7}";
            } else if (path.endsWith(":generateContent")) {
                body = responseJson(request.contains(IN_CHAT) ? OUT_CHAT : OUT_TEXT);
            } else {
                exchange.sendResponseHeaders(404, -1);
                exchange.close();
                return;
            }
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        });
        server.start();
        client = Client.builder().apiKey("offline")
            .httpOptions(HttpOptions.builder()
                .baseUrl("http://127.0.0.1:" + server.getAddress().getPort()).build())
            .build();
    }

    @AfterEach
    void teardown() {
        provider.close();
        server.stop(0);
    }

    private void run(String flag) {
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
        TracedGenerativeModel model = new TracedGenerativeModel(client,
            new FITracer(provider.get("genai-hide-test"), b.build()), "gemini-2.0-flash");
        model.generateContent(IN_PROMPT);
        model.generateContent(List.of(Content.builder().role("user").parts(List.of(Part.fromText(IN_CONTENT))).build()));
        model.countTokens(IN_COUNT);
        model.startChat().sendMessage(IN_CHAT);
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
    void inputFlagsKeepPromptContentOutOfEverySpan(String flag) {
        run(flag);
        for (String s : List.of(IN_PROMPT, IN_CONTENT, IN_COUNT, IN_CHAT)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(leakedKeys(OUT_TEXT)).isNotEmpty();
        assertThat(leakedKeys(OUT_CHAT)).isNotEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) {
        run(flag);
        for (String s : List.of(OUT_TEXT, OUT_CHAT)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        assertThat(leakedKeys(IN_PROMPT)).isNotEmpty();
    }
}
