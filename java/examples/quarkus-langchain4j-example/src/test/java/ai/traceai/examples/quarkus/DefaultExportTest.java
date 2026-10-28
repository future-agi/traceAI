package ai.traceai.examples.quarkus;

import static ai.traceai.examples.quarkus.Exported.AI_SERVICE_SPAN;
import static ai.traceai.examples.quarkus.Exported.awaitTrace;
import static ai.traceai.examples.quarkus.Exported.named;
import static ai.traceai.examples.quarkus.Exported.only;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import ai.traceai.examples.quarkus.Exported.ExportedSpan;
import io.opentelemetry.proto.trace.v1.Status;
import io.quarkus.test.common.QuarkusTestResource;
import io.quarkus.test.junit.QuarkusTest;
import jakarta.inject.Inject;
import java.net.InetAddress;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** The shipped application.properties, unchanged: content off, error text off, no host name. */
@QuarkusTest
@QuarkusTestResource(StandIns.class)
class DefaultExportTest {

    static final String COMPLETION_SPAN = "completion " + StandIns.MODEL;
    static final String TOOL_SPAN = "langchain4j.tools.currentWeather";
    static final List<String> CONTENT_KEYS = List.of("gen_ai.prompt", "gen_ai.completion", "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result");

    @Inject
    Assistant assistant;

    String marker;

    @BeforeEach
    void reset() {
        StandIns.collector.clear();
        StandIns.model.clear();
        marker = "QLC4JMARK" + System.nanoTime();
    }

    @Test
    void oneQuestionIsOneTraceWithTheAiServiceSpanAsRoot() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        List<ExportedSpan> spans = awaitTrace();

        ExportedSpan root = only(spans, AI_SERVICE_SPAN);
        assertTrue(root.parentId().isEmpty(), "AI service span is the root in a command-mode app");
        List<ExportedSpan> completions = named(spans, COMPLETION_SPAN);
        assertEquals(2, completions.size(), Exported.tree(spans));
        completions.forEach(c -> assertEquals(root.spanId(), c.parentId(), "model span is a child of the AI service span"));
        assertEquals(root.spanId(), only(spans, TOOL_SPAN).parentId(), "tool span is a child of the AI service span");
        List<ExportedSpan> http = named(spans, "POST /chat/completions");
        assertEquals(2, http.size(), "one REST client span per model call");
        http.forEach(h -> assertTrue(completions.stream().anyMatch(c -> c.spanId().equals(h.parentId()))));
        assertEquals(1, spans.stream().map(ExportedSpan::traceId).distinct().count());
        assertEquals(1, spans.stream().filter(s -> s.parentId().isEmpty()).count(), "one root");
    }

    @Test
    void modelAndToolSpansCarryGenAiAttributes() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        List<ExportedSpan> spans = awaitTrace();

        List<ExportedSpan> completions = named(spans, COMPLETION_SPAN);
        for (ExportedSpan c : completions) {
            Map<String, String> a = c.attributes();
            assertEquals("chat", a.get("gen_ai.operation.name"));
            assertEquals("open_ai", a.get("gen_ai.provider.name"));
            assertEquals(StandIns.MODEL, a.get("gen_ai.request.model"));
            assertEquals("gpt-test-2026-10-01", a.get("gen_ai.response.model"));
        }
        assertEquals(List.of("31/7", "52/11"), completions.stream()
                .map(c -> c.attributes().get("gen_ai.usage.input_tokens") + "/" + c.attributes().get("gen_ai.usage.output_tokens"))
                .sorted().toList());
        Map<String, String> tool = only(spans, TOOL_SPAN).attributes();
        assertEquals("execute_tool", tool.get("gen_ai.operation.name"));
        assertEquals("currentWeather", tool.get("gen_ai.tool.name"));
    }

    @Test
    void exportIsHttpProtobufToTheTracerRouteWithBothHeadersAndNoGzip() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        awaitTrace();

        List<OtlpRecorder.Post> posts = StandIns.collector.posts();
        assertFalse(posts.isEmpty());
        for (OtlpRecorder.Post post : posts) {
            assertEquals("POST", post.method());
            assertEquals("/tracer/v1/traces", post.path(), "traces only, on the Future AGI route");
            assertEquals("application/x-protobuf", post.headers().get("Content-Type"));
            assertFalse(post.headers().containsKey("Content-Encoding"), "no gzip");
            assertEquals(StandIns.FI_API_KEY, post.headers().get("X-Api-Key"));
            assertEquals(StandIns.FI_SECRET_KEY, post.headers().get("X-Secret-Key"));
        }
    }

    @Test
    void resourceCarriesTheProjectAndNoHostName() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        for (ExportedSpan s : awaitTrace()) {
            Map<String, String> resource = s.resource();
            assertEquals(StandIns.PROJECT, resource.get("project_name"));
            assertEquals("observe", resource.get("project_type"));
            assertEquals("quarkus-langchain4j-example", resource.get("service.name"));
            assertFalse(resource.keySet().stream().anyMatch(k -> k.startsWith("host.") || k.startsWith("process.")),
                    resource.toString());
        }
    }

    @Test
    void contentIsOffByDefault() throws Exception {
        assistant.chat("What is the weather in " + marker + "?");
        List<ExportedSpan> spans = awaitTrace();

        assertTrue(StandIns.model.requests().stream().anyMatch(r -> r.body().contains(marker)), "the model did see it");
        for (ExportedSpan s : spans) {
            for (String key : CONTENT_KEYS) {
                assertFalse(s.attributes().containsKey(key), key + " on " + s.name());
            }
        }
        assertNothingPrivateReceived();
    }

    @Test
    void failedModelCallIsErrorWithoutTheErrorText() throws Exception {
        assertThrows(RuntimeException.class, () -> assistant.chat("What is the weather in MODEL-FAIL-" + marker + "?"));
        List<ExportedSpan> spans = awaitTrace();

        for (ExportedSpan s : List.of(only(spans, AI_SERVICE_SPAN), only(spans, COMPLETION_SPAN))) {
            assertEquals(Status.StatusCode.STATUS_CODE_ERROR, s.span().getStatus().getCode(), s.name());
            assertEquals("", s.span().getStatus().getMessage(), s.name());
        }
        assertTrue(spans.stream().allMatch(s -> s.span().getEventsCount() == 0), "exception events are dropped");
        assertNothingPrivateReceived();
    }

    @Test
    void failedToolIsErrorOnTheToolSpan() throws Exception {
        // The tool throws "No weather station for Atlantis-<marker>". Quarkus LangChain4j sends that text back to the
        // model as the tool result, so the AI service call itself succeeds.
        assistant.chat("What is the weather in Atlantis-" + marker + "?");
        List<ExportedSpan> spans = awaitTrace();

        ExportedSpan tool = only(spans, TOOL_SPAN);
        assertEquals(Status.StatusCode.STATUS_CODE_ERROR, tool.span().getStatus().getCode());
        assertEquals("", tool.span().getStatus().getMessage());
        assertEquals(Status.StatusCode.STATUS_CODE_UNSET, only(spans, AI_SERVICE_SPAN).span().getStatus().getCode());
        assertEquals(0, tool.span().getEventsCount());
        assertNothingPrivateReceived();
    }

    @Test
    void responsesWithoutUsageGetNoTokenAttributes() throws Exception {
        assistant.chat("NO-USAGE What is the weather in Lisbon?");
        for (ExportedSpan c : named(awaitTrace(), COMPLETION_SPAN)) {
            assertFalse(c.attributes().keySet().stream().anyMatch(k -> k.startsWith("gen_ai.usage.")), c.attributes().toString());
        }
    }

    @Test
    void futureAgiKeysOnlyGoToTheCollector() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        awaitTrace();
        for (MockOpenAi.Request r : StandIns.model.requests()) {
            String all = r.headers() + r.body();
            assertFalse(all.contains(StandIns.FI_API_KEY) || all.contains(StandIns.FI_SECRET_KEY));
            assertEquals("Bearer " + StandIns.PROVIDER_KEY, r.headers().get("Authorization"));
        }
    }

    /** Scans every byte the collector received, headers included. */
    private void assertNothingPrivateReceived() throws Exception {
        String received = Exported.everythingReceived();
        assertFalse(received.contains(marker), "marker exported");
        assertFalse(received.contains(StandIns.PROVIDER_KEY), "provider key exported");
        assertFalse(received.contains("\tat ") || received.contains("Exception"), "stack trace or exception type exported");
        assertFalse(received.contains("upstream rejected") || received.contains("No weather station"), "error text exported");
        assertFalse(received.contains(InetAddress.getLocalHost().getHostName()), "host name exported");
        assertFalse(received.contains(System.getProperty("user.home")), "home directory exported");
        assertFalse(received.contains(System.getProperty("user.dir")), "working directory exported");
        assertFalse(received.contains("/" + System.getProperty("user.name") + "/"), "user name exported");
    }
}
