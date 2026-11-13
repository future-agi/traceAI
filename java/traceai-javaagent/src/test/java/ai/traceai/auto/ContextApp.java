package ai.traceai.auto;

import ai.traceai.ContextAttributes;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;

/** Uses the application's UNRELOCATED OTel API, not the agent's SDK. */
public final class ContextApp {
    static void run(String[] args) throws Exception {
        SdkTracerProvider provider = SdkTracerProvider.builder().build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        Span parent = GlobalOpenTelemetry.getTracer("test-application").spanBuilder("application-parent").startSpan();
        parent.setAttribute("gen_ai.span.kind", args[2].startsWith("dedup") ? "LLM" : "CHAIN");
        System.out.println("PARENT=" + parent.getSpanContext().getSpanId());
        try (var scope = parent.makeCurrent(); var session = ContextAttributes.usingSession("e2e-session")) {
            if (args[2].contains("native")) {
                Span nativeSpan = GlobalOpenTelemetry.getTracer("native-sdk").spanBuilder("native-client").startSpan();
                try (var nativeScope = nativeSpan.makeCurrent()) { PlainApp.call(args); } finally { nativeSpan.end(); }
            } else PlainApp.call(args);
            if (args[2].contains("privacy")) {
                String echoed = "PROVIDER_CREDENTIAL_987654321";
                parent.setStatus(io.opentelemetry.api.trace.StatusCode.ERROR, "echo " + echoed);
                parent.recordException(new java.io.IOException("echo " + echoed + " {\"password\":\"JSON_PASSWORD_PRIVATE_123\"}"));
            }
        }
        finally { parent.end(); provider.shutdown(); }
    }
}
