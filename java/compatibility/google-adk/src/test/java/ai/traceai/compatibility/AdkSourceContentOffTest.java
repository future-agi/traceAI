package ai.traceai.compatibility;

import static org.junit.jupiter.api.Assertions.*;

import ai.traceai.nativeotel.GoogleAdkTelemetry;
import com.google.adk.telemetry.Tracing;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

@EnabledIfEnvironmentVariable(named = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", matches = "false")
class AdkSourceContentOffTest {
    @Test void destinationOptInCannotRestoreContentDisabledByAdk() {
        List<SpanData> spans = new CopyOnWriteArrayList<>();
        SpanExporter exporter = new SpanExporter() {
            @Override public CompletableResultCode export(Collection<SpanData> batch) { spans.addAll(batch); return CompletableResultCode.ofSuccess(); }
            @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        var sdk = GoogleAdkTelemetry.register(new GoogleAdkTelemetry.Config(
                "http://localhost:4318", "key", "secret", "project"), exporter, true);
        var span = Tracing.getTracer().spanBuilder("execute_tool weather").startSpan();
        Tracing.traceToolExecution(span, "weather", "weather", "FunctionTool",
                Map.of("city", "private-city"), null, null);
        span.end();
        GoogleAdkTelemetry.shutdown(sdk);
        assertEquals(1, spans.size());
        var attributes = spans.get(0).getAttributes();
        assertEquals("{}", attributes.get(AttributeKey.stringKey("gcp.vertex.agent.tool_call_args")));
        assertEquals("{}", attributes.get(AttributeKey.stringKey("input.value")));
        assertFalse(attributes.toString().contains("private-city"));
    }
}
