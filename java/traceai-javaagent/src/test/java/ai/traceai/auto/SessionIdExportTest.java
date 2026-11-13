package ai.traceai.auto;

import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/** The session id set through the context API is an identifier: exported as set, apart from secrets inside it. */
class SessionIdExportTest {
    static Map<String, Object> exported(String scope, Map<String, String> attributes) {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get(scope).spanBuilder("chat test-model").startSpan();
            attributes.forEach(span::setAttribute);
            span.end();
        }
        SpanData data = memory.getFinishedSpanItems().get(0);
        Object json = new OtlpJsonExporter(AutoConfig.resolve(Map.of(), Map.of())).serialize(data);
        Object span = WireCore.first(WireCore.get(WireCore.first(WireCore.get(json, "scopeSpans")), "spans"));
        Map<String, Object> result = new LinkedHashMap<>();
        for (Object kv : WireCore.list(WireCore.get(span, "attributes"))) result.put(WireCore.str(WireCore.get(kv, "key")), WireCore.get(WireCore.get(kv, "value"), "stringValue"));
        return result;
    }
    @Test void agentSpansExportTheContextSessionIdAsSetUnderBothKeys() {
        var attrs = exported("traceai-java-auto", Map.of("session.id", "checkout-session-42", "gen_ai.conversation.id", "checkout-session-42"));
        assertEquals("checkout-session-42", attrs.get("session.id")); assertEquals("checkout-session-42", attrs.get("gen_ai.conversation.id"));
    }
    @Test void knownSecretsAndCredentialShapesInsideTheSessionIdAreStillRedactedUnderBothKeys() {
        TraceAIAuto.registerSecret("SESSION_REGISTERED_PRIVATE_VALUE");
        for (String value : List.of("visit SESSION_REGISTERED_PRIVATE_VALUE", "visit Bearer SESSIONSHAPEPRIVATE1")) {
            var attrs = exported("traceai-java-auto", Map.of("session.id", value, "gen_ai.conversation.id", value));
            for (String key : List.of("session.id", "gen_ai.conversation.id")) {
                assertEquals("visit <redacted>", attrs.get(key), key + " from " + value);
            }
        }
    }
    @Test void otherSessionNamesKeepTheirRedaction() {
        var agent = exported("traceai-java-auto", Map.of("session_id", "PRIVATE_SESSION_A", "metadata.session.id", "PRIVATE_SESSION_B", "http.request.header.x-session-id", "PRIVATE_SESSION_C"));
        assertEquals(3, agent.size(), agent.toString());
        for (var value : agent.values()) assertEquals("<redacted>", value);
        assertEquals("<redacted>", exported("application", Map.of("session.id", "PRIVATE_SESSION_D")).get("session.id"));
    }
}
