package ai.traceai.examples.quarkus;

import static ai.traceai.examples.quarkus.DefaultExportTest.COMPLETION_SPAN;
import static ai.traceai.examples.quarkus.Exported.AI_SERVICE_SPAN;
import static ai.traceai.examples.quarkus.Exported.awaitTrace;
import static ai.traceai.examples.quarkus.Exported.only;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import ai.traceai.examples.quarkus.Exported.ExportedSpan;
import io.opentelemetry.proto.trace.v1.Span;
import io.opentelemetry.proto.trace.v1.Status;
import io.quarkus.test.common.QuarkusTestResource;
import io.quarkus.test.junit.QuarkusTest;
import io.quarkus.test.junit.QuarkusTestProfile;
import io.quarkus.test.junit.TestProfile;
import jakarta.inject.Inject;
import java.net.InetAddress;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * What Quarkus and Quarkus LangChain4j do without this recipe's helpers and limits. These are the reasons the
 * helpers exist; a failure here after an upgrade means upstream changed and the docs need a re-check.
 */
@QuarkusTest
@QuarkusTestResource(StandIns.class)
@TestProfile(UpstreamDefaultsTest.Upstream.class)
class UpstreamDefaultsTest {

    public static class Upstream implements QuarkusTestProfile {
        @Override
        public Map<String, String> getConfigOverrides() {
            return Map.of(
                    "quarkus.arc.exclude-types", "FutureAgiTracingDefaults,DropHostName,ToolErrorStatus",
                    "quarkus.otel.span.event.count.limit", "128",
                    // The full traces URL, as other OTLP SDKs take it. Quarkus appends /v1/traces again.
                    "quarkus.otel.exporter.otlp.traces.endpoint", "${FI_BASE_URL}/tracer/v1/traces");
        }
    }

    @Inject
    Assistant assistant;

    @BeforeEach
    void reset() {
        StandIns.collector.clear();
    }

    @Test
    void quarkusAddsTheHostNameToTheResource() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        String hostName = InetAddress.getLocalHost().getHostName();
        assertTrue(awaitTrace().stream().allMatch(s -> hostName.equals(s.resource().get("host.name"))));
    }

    @Test
    void upstreamLeavesFailuresUnsetAndExportsTheErrorTextAndStackTrace() throws Exception {
        String marker = "QLC4JUP" + System.nanoTime();
        assertThrows(RuntimeException.class, () -> assistant.chat("What is the weather in MODEL-FAIL-" + marker + "?"));
        List<ExportedSpan> spans = awaitTrace();

        for (ExportedSpan s : List.of(only(spans, AI_SERVICE_SPAN), only(spans, COMPLETION_SPAN))) {
            assertEquals(Status.StatusCode.STATUS_CODE_UNSET, s.span().getStatus().getCode(), s.name());
            Span.Event exception = s.span().getEventsList().stream().filter(e -> e.getName().equals("exception")).findFirst().orElseThrow();
            Map<String, String> attributes = Exported.toMap(exception.getAttributesList());
            assertTrue(attributes.get("exception.message").contains(marker), s.name());
            assertTrue(attributes.get("exception.stacktrace").contains("\tat "), s.name());
        }
    }

    @Test
    void anEndpointEndingInV1TracesIsPostedToADoubledPath() throws Exception {
        assistant.chat("What is the weather in Lisbon?");
        awaitTrace();
        assertTrue(StandIns.collector.posts().stream().allMatch(p -> p.path().equals("/tracer/v1/traces/v1/traces")));
    }
}
