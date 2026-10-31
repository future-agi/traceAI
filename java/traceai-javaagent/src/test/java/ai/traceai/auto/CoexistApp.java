package ai.traceai.auto;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import java.util.Map;

/** Verifies both premain and attachment to an SDK/tracer constructed before agent initialization. */
public final class CoexistApp {
    static void run(String[] args) throws Exception {
        var memory = InMemorySpanExporter.create();
        var provider = SdkTracerProvider.builder().setResource(io.opentelemetry.sdk.resources.Resource.create(io.opentelemetry.api.common.Attributes.of(io.opentelemetry.api.common.AttributeKey.stringKey("service.name"), "real-application-service")))
            .addSpanProcessor(SimpleSpanProcessor.create(memory)).build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        var tracer = GlobalOpenTelemetry.getTracer("real-application-scope", "2.3.4");
        Class<?> agent = Class.forName("ai.traceai.auto.TraceAIAuto");
        try {
            for (int i = 0; i < 3; i++) agent.getMethod("init", Map.class).invoke(null, Map.of());
            var link = io.opentelemetry.api.trace.SpanContext.create("12345678901234567890123456789012", "1234567890123456", io.opentelemetry.api.trace.TraceFlags.getSampled(), io.opentelemetry.api.trace.TraceState.getDefault());
            var span = tracer.spanBuilder("application-library-call").addLink(link).startSpan(); span.setAttribute("gen_ai.span.kind", "LLM");
            span.setAttribute("fi.raw_input", "PRIVATE_INPUT"); span.setAttribute("fi.raw_output", "PRIVATE_OUTPUT");
            span.setAttribute("password", "ATTRIBUTE_PRIVATE_PASSWORD");
            span.setStatus(io.opentelemetry.api.trace.StatusCode.ERROR, "echo test-api-key https://x/?sig=STATUS_PRIVATE_TOKEN");
            span.recordException(new IllegalStateException("echo test-api-key https://x/?sig=EVENT_PRIVATE_TOKEN"));
            span.addEvent("diagnostic", io.opentelemetry.api.common.Attributes.of(io.opentelemetry.api.common.AttributeKey.stringKey("api_key"), "EVENT_PRIVATE_KEY"));
            try (var ignored = span.makeCurrent()) { PlainApp.call(args); } finally { span.end(); }
            if (memory.getFinishedSpanItems().size() != 1) throw new AssertionError("Existing application exporter did not receive exactly one span");
            var own = memory.getFinishedSpanItems().get(0);
            if (own.getEvents().size() != 2 || own.getLinks().size() != 1 || !"PRIVATE_INPUT".equals(own.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("fi.raw_input")))) throw new AssertionError("Agent changed application span data");
            agent.getMethod("flush").invoke(null); agent.getMethod("shutdown").invoke(null);
            tracer.spanBuilder("application-after-shutdown").startSpan().end();
            if (memory.getFinishedSpanItems().size() != 2) throw new AssertionError("Agent took ownership of application SDK");
            System.out.println("APP_PROVIDER_ALIVE=true"); System.out.println("LIBRARY_SPAN=" + span.getSpanContext().getSpanId());
        } finally { provider.shutdown(); }
    }
}
