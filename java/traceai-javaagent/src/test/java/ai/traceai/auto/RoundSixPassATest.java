package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundSixPassATest {
    @Test void tracestateEntriesAreScrubbedBeforeFramingOnSpansAndLinks() {
        String png = "iVBORw0KGgoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
        TraceState state = TraceState.builder().put("vendor", png).put("token", "TRACESTATE_PRIVATE_81973264").put("audio", "QUJD").put("public", "keep").build();
        SpanContext parent = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), state);
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("r6a").spanBuilder("state").setParent(Context.root().with(Span.wrap(parent))).addLink(parent).startSpan();
            span.end();
            var original = memory.getFinishedSpanItems().get(0);
            String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of(), Map.of())).serialize(original));
            assertFalse(json.contains(png)); assertFalse(json.contains("TRACESTATE_PRIVATE_81973264")); assertFalse(json.contains("QUJD"));
            assertTrue(json.contains("public=keep")); assertTrue(json.contains("vendor=<omitted: 42 base64 chars>"));
            assertTrue(json.contains("token=<redacted>")); assertTrue(json.contains("audio=<omitted: 4 base64 chars>"));
            assertEquals(state, original.getSpanContext().getTraceState()); assertEquals(state, original.getLinks().get(0).getSpanContext().getTraceState());
        }
    }
}
