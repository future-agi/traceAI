package ai.traceai;

import io.opentelemetry.api.trace.Span;

/** Child-JVM entry point for AmbientHeadersTest: one span through the normal TraceAI setup. */
public final class AmbientHeadersChild {
    public static void main(String[] args) {
        TraceAI.init(TraceConfig.builder().baseUrl(args[0])
            .apiKey("synthetic-api-key").secretKey("synthetic-secret-key")
            .projectName("ambient-headers-project").exportIntervalMs(60000).build());
        FITracer tracer = TraceAI.getTracer();
        Span span = tracer.startSpan("ambient-headers-span", FISpanKind.LLM);
        span.end();
        TraceAI.shutdown();
    }
}
