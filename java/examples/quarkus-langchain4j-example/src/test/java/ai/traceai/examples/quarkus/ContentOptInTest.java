package ai.traceai.examples.quarkus;

import static ai.traceai.examples.quarkus.DefaultExportTest.COMPLETION_SPAN;
import static ai.traceai.examples.quarkus.DefaultExportTest.TOOL_SPAN;
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
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import jakarta.inject.Inject;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** Conformance profile: the documented opt-ins turned on. Not a production setting. */
@QuarkusTest
@QuarkusTestResource(StandIns.class)
@TestProfile(ContentOptInTest.OptIn.class)
class ContentOptInTest {

    public static class OptIn implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of(
                    "quarkus.langchain4j.tracing.include-prompt", "true",
                    "quarkus.langchain4j.tracing.include-completion", "true",
                    "quarkus.langchain4j.tracing.include-tool-arguments", "true",
                    "quarkus.langchain4j.tracing.include-tool-result", "true",
                    "fi.export-error-details", "true");
        }
    }

    @Inject
    Assistant assistant;

    String marker;

    @BeforeEach
    void reset() {
        StandIns.collector.clear();
        marker = "QLC4JOPTIN" + System.nanoTime();
    }

    @Test
    void optInExportsPromptCompletionToolArgumentsAndToolResult() throws Exception {
        assistant.chat("What is the weather in " + marker + "?");
        List<ExportedSpan> spans = awaitTrace();

        List<ExportedSpan> completions = named(spans, COMPLETION_SPAN);
        assertTrue(completions.stream().allMatch(c -> c.attribute("gen_ai.prompt").orElse("").contains(marker)),
                Exported.tree(spans));
        assertTrue(completions.stream().anyMatch(c -> c.attribute("gen_ai.completion").orElse("").contains(marker)));
        Map<String, String> tool = only(spans, TOOL_SPAN).attributes();
        assertEquals("{\"city\":\"" + marker + "\"}", tool.get("gen_ai.tool.call.arguments"));
        assertEquals("Sunny, 21 C in " + marker, tool.get("gen_ai.tool.call.result"));
        assertFalse(Exported.everythingReceived().contains(StandIns.PROVIDER_KEY));
    }

    @Test
    void optInPutsTheErrorMessageInTheSpanStatus() throws Exception {
        assertThrows(RuntimeException.class, () -> assistant.chat("What is the weather in MODEL-FAIL-" + marker + "?"));
        List<ExportedSpan> spans = awaitTrace();

        for (ExportedSpan s : List.of(only(spans, AI_SERVICE_SPAN), only(spans, COMPLETION_SPAN))) {
            assertEquals(Status.StatusCode.STATUS_CODE_ERROR, s.span().getStatus().getCode());
            assertTrue(s.span().getStatus().getMessage().contains(marker), s.span().getStatus().getMessage());
        }
        // Exception events stay off: that is quarkus.otel.span.event.count.limit, a separate switch.
        assertTrue(spans.stream().allMatch(s -> s.span().getEventsCount() == 0));
        assertTrue(spans.stream().noneMatch(s -> s.resource().containsKey("host.name")));
    }
}
