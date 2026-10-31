package ai.futureagi.examples.embabel;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.ResourceSpans;
import io.opentelemetry.proto.trace.v1.Span;
import io.opentelemetry.proto.trace.v1.Status;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.ConfigurableApplicationContext;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Runs the example app exactly as configured in application.yml, with the two remote ends
 * replaced by in-process stubs: an OTLP/HTTP receiver standing in for Future AGI and an
 * OpenAI-compatible chat completions stub standing in for the model. Checks what leaves the
 * process: path, headers, compression, resource, span tree, and content with capture off and on.
 */
class RecipeContractTest {

    private static final String API_KEY = "fixture-api-key-" + UUID.randomUUID();
    private static final String SECRET_KEY = "fixture-secret-key-" + UUID.randomUUID();
    private static final String MODEL_KEY = "sk-fixture-model-key-" + UUID.randomUUID();
    private static final String MARK = "CONTRACT" + UUID.randomUUID().toString().replace("-", "");

    record Received(String path, Map<String, List<String>> headers, ExportTraceServiceRequest body, String raw) {
    }

    private HttpServer otlp;
    private HttpServer model;
    private final List<Received> received = Collections.synchronizedList(new ArrayList<>());

    @BeforeEach
    void startStubs() throws IOException {
        otlp = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        otlp.createContext("/", exchange -> {
            try {
                byte[] body = exchange.getRequestBody().readAllBytes();
                ExportTraceServiceRequest request = ExportTraceServiceRequest.parseFrom(body);
                received.add(new Received(exchange.getRequestURI().getPath(), lowerCaseHeaders(exchange),
                        request, request.toString() + new String(body, StandardCharsets.ISO_8859_1)));
                respond(exchange, 200, "application/x-protobuf", new byte[0]);
            } catch (RuntimeException | Error e) {
                System.err.println("OTLP stub failed: " + e + " cause: " + e.getCause());
                respond(exchange, 500, "text/plain", new byte[0]);
            }
        });
        otlp.start();

        model = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        model.createContext("/v1/chat/completions", exchange -> {
            String request = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            respond(exchange, 200, "application/json", fakeCompletion(request).getBytes(StandardCharsets.UTF_8));
        });
        model.start();
    }

    @AfterEach
    void stopStubs() {
        otlp.stop(0);
        model.stop(0);
    }

    /** Tool call first; once the tool result is in the conversation, the final JSON answer. */
    static String fakeCompletion(String request) {
        Matcher station = Pattern.compile("stationId \\\\\"(.*?)\\\\\"").matcher(request);
        String stationId = station.find() ? station.group(1) : "unknown";
        String usage = "\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}";
        if (!request.contains("\"role\":\"tool\"")) {
            return """
                    {"id":"chatcmpl-1","object":"chat.completion","created":1,"model":"stub-model",
                     "choices":[{"index":0,"finish_reason":"tool_calls","message":{"role":"assistant","content":null,
                       "tool_calls":[{"id":"call_1","type":"function","function":{"name":"currentWeather",
                         "arguments":"{\\"stationId\\":\\"%s\\"}"}}]}}],%s}
                    """.formatted(stationId, usage);
        }
        return """
                {"id":"chatcmpl-2","object":"chat.completion","created":2,"model":"stub-model",
                 "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant",
                   "content":"{\\"stationId\\":\\"%s\\",\\"summary\\":\\"Clear sky at %s.\\"}"}}],%s}
                """.formatted(stationId, stationId, usage);
    }

    private void runApp(String stationId, boolean captureContent) {
        GlobalOpenTelemetry.resetForTest(); // Embabel registers the SDK globally once per JVM.
        String[] args = {
                "--FI_PROJECT_NAME=contract-project",
                "--FI_BASE_URL=http://127.0.0.1:" + otlp.getAddress().getPort(),
                "--FI_API_KEY=" + API_KEY,
                "--FI_SECRET_KEY=" + SECRET_KEY,
                "--OPENAI_CUSTOM_BASE_URL=http://127.0.0.1:" + model.getAddress().getPort() + "/v1",
                "--OPENAI_CUSTOM_API_KEY=" + MODEL_KEY,
                "--OPENAI_CUSTOM_MODELS=stub-model",
                "--EMBABEL_MODELS_DEFAULT_LLM=stub-model",
                // An EMBABEL_MODELS_DEFAULT_LLM environment variable also binds to this property
                // directly, so set it explicitly to keep the test independent of the shell.
                "--embabel.models.default-llm=stub-model",
                "--EMBABEL_CAPTURE_CONTENT=" + captureContent,
                "--demo.station-id=" + stationId,
                "--demo.question=Is it a good day for a walk near " + MARK + "?",
        };
        // Closing the context is the documented shutdown path: it flushes the batch span processor.
        try (ConfigurableApplicationContext ignored = new SpringApplicationBuilder(EmbabelTracingApplication.class)
                .run(args)) {
            assertThat(ignored.isActive()).isTrue();
        }
        assertThat(received).as("OTLP requests received").isNotEmpty();
    }

    private List<Span> spans() {
        return received.stream().flatMap(r -> r.body().getResourceSpansList().stream())
                .flatMap(rs -> rs.getScopeSpansList().stream()).flatMap(ss -> ss.getSpansList().stream()).toList();
    }

    private Span span(String name) {
        return spans().stream().filter(s -> s.getName().equals(name)).findFirst()
                .orElseThrow(() -> new AssertionError("no span named " + name + " in "
                        + spans().stream().map(Span::getName).toList()));
    }

    private static Map<String, String> attributes(List<KeyValue> kvs) {
        return kvs.stream().collect(Collectors.toMap(KeyValue::getKey, kv -> kv.getValue().toString().trim(), (a, b) -> a));
    }

    private String everything() {
        return received.stream().map(Received::raw).collect(Collectors.joining("\n"));
    }

    @Test
    void contentOffRunReachesTheEndpointWithHeadersProjectAndTree() {
        runApp("LIS-" + MARK, false);

        for (Received r : received) {
            assertThat(r.path()).isEqualTo("/tracer/v1/traces");
            assertThat(r.headers().get("x-api-key")).containsExactly(API_KEY);
            assertThat(r.headers().get("x-secret-key")).containsExactly(SECRET_KEY);
            assertThat(r.headers().get("content-type")).containsExactly("application/x-protobuf");
            assertThat(r.headers()).doesNotContainKey("content-encoding");
            for (ResourceSpans rs : r.body().getResourceSpansList()) {
                Map<String, String> resource = attributes(rs.getResource().getAttributesList());
                assertThat(resource).containsEntry("project_name", "string_value: \"contract-project\"")
                        .containsEntry("project_type", "string_value: \"observe\"")
                        .containsEntry("service.name", "string_value: \"embabel-weather-agent\"");
                assertThat(resource.keySet()).noneMatch(k -> k.startsWith("host.") || k.startsWith("process.")
                        || k.startsWith("os."));
            }
        }

        Span agent = span("agent WeatherAgent");
        Span action = span("action report");
        Span llm = span("llm stub-model");
        Span loop = span("tool-loop");
        Span tool = span("execute_tool currentWeather");
        List<Span> chats = spans().stream().filter(s -> s.getName().equals("chat stub-model")).toList();
        assertThat(agent.getParentSpanId().isEmpty()).isTrue();
        assertThat(action.getParentSpanId()).isEqualTo(agent.getSpanId());
        assertThat(llm.getParentSpanId()).isEqualTo(action.getSpanId());
        assertThat(loop.getParentSpanId()).isEqualTo(llm.getSpanId());
        assertThat(tool.getParentSpanId()).isEqualTo(loop.getSpanId());
        assertThat(chats).hasSize(2).allSatisfy(c -> {
            assertThat(c.getParentSpanId()).isEqualTo(loop.getSpanId());
            assertThat(attributes(c.getAttributesList())).containsEntry("gen_ai.operation.name", "string_value: \"chat\"")
                    .containsEntry("gen_ai.request.model", "string_value: \"stub-model\"")
                    // Micrometer tags are strings, so token counts arrive string-typed.
                    .containsEntry("gen_ai.usage.input_tokens", "string_value: \"11\"")
                    .containsEntry("gen_ai.usage.output_tokens", "string_value: \"7\"");
        });
        assertThat(attributes(tool.getAttributesList())).containsEntry("gen_ai.tool.name", "string_value: \"currentWeather\"");
        assertThat(spans()).allSatisfy(s -> assertThat(s.getTraceId()).isEqualTo(agent.getTraceId()));
        assertThat(spans()).noneMatch(s -> s.getStatus().getCode() == Status.StatusCode.STATUS_CODE_ERROR);

        assertThat(everything()).doesNotContain(MARK).doesNotContain(API_KEY).doesNotContain(SECRET_KEY)
                .doesNotContain(MODEL_KEY).doesNotContain(System.getProperty("user.home"));
    }

    @Test
    void contentOnRunCarriesPromptsAndResults() {
        runApp("LIS-" + MARK, true);
        assertThat(everything()).contains(MARK).doesNotContain(API_KEY).doesNotContain(SECRET_KEY)
                .doesNotContain(MODEL_KEY);
        assertThat(attributes(span("action report").getAttributesList())).containsKeys("input.value", "output.value");
    }

    @Test
    void toolFailureIsAnErrorSpanWithoutErrorTextWhenContentIsOff() {
        runApp("offline-" + MARK, false);
        Span tool = span("execute_tool currentWeather");
        assertThat(tool.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
        assertThat(tool.getStatus().getMessage()).isEmpty();
        assertThat(attributes(tool.getAttributesList())).containsEntry("embabel.tool.status", "string_value: \"error\"")
                .containsKey("embabel.tool.error.type").doesNotContainKey("embabel.tool.error.message");
        assertThat(tool.getEventsList()).allSatisfy(e ->
                assertThat(attributes(e.getAttributesList())).doesNotContainKeys("exception.message", "exception.stacktrace"));
        assertThat(everything()).doesNotContain(MARK).doesNotContain("is offline");
    }

    @Test
    void toolFailureKeepsErrorTextWhenContentIsOn() {
        runApp("offline-" + MARK, true);
        Span tool = span("execute_tool currentWeather");
        assertThat(tool.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
        assertThat(everything()).contains("is offline");
        // Stack traces stay out even with content on.
        assertThat(spans()).allSatisfy(s -> assertThat(s.getEventsList()).allSatisfy(e ->
                assertThat(attributes(e.getAttributesList())).doesNotContainKey("exception.stacktrace")));
        assertThat(everything()).doesNotContain("WeatherTools.java");
    }

    private static Map<String, List<String>> lowerCaseHeaders(HttpExchange exchange) {
        return exchange.getRequestHeaders().entrySet().stream()
                .collect(Collectors.toMap(e -> e.getKey().toLowerCase(), Map.Entry::getValue, (a, b) -> a));
    }

    private static void respond(HttpExchange exchange, int status, String contentType, byte[] body) throws IOException {
        exchange.getResponseHeaders().add("Content-Type", contentType);
        exchange.sendResponseHeaders(status, body.length == 0 ? -1 : body.length);
        if (body.length > 0) {
            exchange.getResponseBody().write(body);
        }
        exchange.close();
    }
}
