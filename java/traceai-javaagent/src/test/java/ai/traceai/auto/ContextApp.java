package ai.traceai.auto;

import ai.traceai.ContextAttributes;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;

/** Uses the application's UNRELOCATED OTel API, not the agent's SDK. */
public final class ContextApp {
    // The pinned SDK builder rejects zero, but application-provided SpanLimits can expose it.
    static io.opentelemetry.sdk.trace.SpanLimits limits(int attributes) {
        var defaults = io.opentelemetry.sdk.trace.SpanLimits.getDefault();
        return new io.opentelemetry.sdk.trace.SpanLimits() {
            @Override public int getMaxNumberOfAttributes() { return attributes; }
            @Override public int getMaxNumberOfEvents() { return defaults.getMaxNumberOfEvents(); }
            @Override public int getMaxNumberOfLinks() { return defaults.getMaxNumberOfLinks(); }
            @Override public int getMaxNumberOfAttributesPerEvent() { return defaults.getMaxNumberOfAttributesPerEvent(); }
            @Override public int getMaxNumberOfAttributesPerLink() { return defaults.getMaxNumberOfAttributesPerLink(); }
        };
    }
    static void run(String[] args) throws Exception {
        if (args[2].startsWith("framework_ratio_remote")) { remoteRatio(args); return; }
        boolean ratioBudget = args[2].equals("framework_ratio_budget");
        var builder = SdkTracerProvider.builder();
        if (ratioBudget) builder.setSpanLimits(limits(Integer.parseInt(System.getenv("TRACEAI_TEST_ATTRIBUTE_LIMIT"))));
        SdkTracerProvider provider = builder.build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        var parentBuilder = GlobalOpenTelemetry.getTracer("test-application").spanBuilder("application-parent");
        if (ratioBudget) {
            var remote = io.opentelemetry.api.trace.SpanContext.createFromRemoteParent(
                "00000000000000000000000000000001", "0000000000000001",
                io.opentelemetry.api.trace.TraceFlags.getSampled(),
                io.opentelemetry.api.trace.TraceState.builder().put("vendor", "value").put("fi", "r:0.25").build());
            parentBuilder.setParent(io.opentelemetry.context.Context.root().with(Span.wrap(remote)));
        }
        Span parent = parentBuilder.startSpan();
        parent.setAttribute("gen_ai.span.kind", args[2].startsWith("dedup") ? "LLM" : "CHAIN");
        System.out.println("PARENT=" + parent.getSpanContext().getSpanId());
        try (var scope = parent.makeCurrent(); var session = ContextAttributes.usingSession("e2e-session")) {
            if (args[2].contains("native")) {
                Span nativeSpan = GlobalOpenTelemetry.getTracer("native-sdk").spanBuilder("native-client").startSpan();
                System.out.println("NATIVE=" + nativeSpan.getSpanContext().getSpanId());
                try (var nativeScope = nativeSpan.makeCurrent()) { PlainApp.call(args); } finally { nativeSpan.end(); }
            } else PlainApp.call(args);
            if (args[2].contains("privacy")) {
                String echoed = "PROVIDER_CREDENTIAL_987654321";
                parent.setStatus(io.opentelemetry.api.trace.StatusCode.ERROR, "echo " + echoed);
                parent.recordException(new java.io.IOException("echo " + echoed + " {\"password\":\"JSON_PASSWORD_PRIVATE_123\"}"));
            }
        }
        finally { parent.end(); }
        try {
            // A control call outside every application span: the agent is running and traces it.
            if (args[2].startsWith("dedup")) PlainApp.call(args);
        } finally { provider.shutdown(); }
    }

    /** Cross the shaded bridge with a remote parent, then with its application-owned local child. */
    private static void remoteRatio(String[] args) throws Exception {
        boolean sampled = !args[2].endsWith("unsampled");
        var state = io.opentelemetry.api.trace.TraceState.builder().put("vendor", "value").put("fi", "r:0.5").build();
        var remote = io.opentelemetry.api.trace.SpanContext.createFromRemoteParent(
            "00000000000000000000000000000001", "0000000000000001",
            sampled ? io.opentelemetry.api.trace.TraceFlags.getSampled() : io.opentelemetry.api.trace.TraceFlags.getDefault(), state);
        var context = io.opentelemetry.context.Context.root().with(Span.wrap(remote));
        var memory = io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder()
                .addSpanProcessor(io.opentelemetry.sdk.trace.export.SimpleSpanProcessor.create(memory)).build()) {
            try (var scope = context.makeCurrent()) { PlainApp.call(args); }
            Span local = provider.get("test-application").spanBuilder("application-parent").setParent(context).startSpan();
            System.out.println("LOCAL_PARENT=" + local.getSpanContext().getSpanId());
            try (var scope = local.makeCurrent()) { PlainApp.call(args); } finally { local.end(); }
            if (!remote.getTraceState().equals(state) || !local.getSpanContext().getTraceState().equals(state)
                    || local.getSpanContext().isSampled() != sampled) throw new AssertionError("Application parent changed");
            if (memory.getFinishedSpanItems().size() != (sampled ? 1 : 0)) throw new AssertionError("Application sampling changed");
            if (sampled && memory.getFinishedSpanItems().get(0).getAttributes()
                    .get(io.opentelemetry.api.common.AttributeKey.doubleKey("fi.sampling.ratio")) != null)
                throw new AssertionError("Application provider was stamped");
            System.out.println("APPLICATION_PARENT_UNCHANGED=true");
        }
    }
}
