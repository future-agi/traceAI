package ai.traceai.auto;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.Span;

/** The documented wrapper setup, TraceAI.init with Future AGI keys, running beside -javaagent. */
public final class WrapperInitApp {
    static void run(String[] args) throws Exception {
        TraceAI.init(TraceConfig.builder().baseUrl(System.getenv("FI_BASE_URL")).apiKey(System.getenv("FI_API_KEY"))
            .secretKey(System.getenv("FI_SECRET_KEY")).projectName("wrapper-project").build());
        FITracer tracer = TraceAI.getTracer();
        Span span = tracer.startSpan("wrapper-llm-call", FISpanKind.LLM);
        try (var scope = span.makeCurrent()) { PlainApp.call(args); } finally { span.end(); }
        System.out.println("WRAPPER_SPAN=" + span.getSpanContext().getSpanId());
        // Flushes and closes the wrapper's own provider and exporter.
        TraceAI.shutdown();
        Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("flush").invoke(null);
    }
}
