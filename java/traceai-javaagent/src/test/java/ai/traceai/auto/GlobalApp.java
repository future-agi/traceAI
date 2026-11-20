package ai.traceai.auto;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.Map;

/**
 * What GlobalOpenTelemetry is for application code once the agent runs. The agent's classes are loaded unshaded here
 * (no -javaagent jar), so its OpenTelemetry API is the application's: whatever the agent registers globally, the
 * application sees. The answer is the application's own provider, else the no-op default.
 */
public final class GlobalApp {
    static void run(String[] args) throws Exception {
        boolean own = args[2].equals("global_app");
        InMemorySpanExporter memory = InMemorySpanExporter.create();
        if (own) {
            var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build();
            OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        }
        Class<?> agent = Class.forName("ai.traceai.auto.TraceAIAuto");
        agent.getMethod("init", Map.class).invoke(null, Map.of());
        var span = GlobalOpenTelemetry.getTracer("app-library-global").spanBuilder("global-app-span").startSpan();
        System.out.println("GLOBAL_SPAN_RECORDING=" + span.isRecording());
        span.setAttribute("gen_ai.span.kind", "LLM"); // an AI span: the filter that keeps other spans local would let it through
        span.end();
        PlainApp.call(args); // a real AI call: the agent traces it
        agent.getMethod("flush").invoke(null); agent.getMethod("shutdown").invoke(null);
        if (own) System.out.println("APP_EXPORTER_SPANS=" + memory.getFinishedSpanItems().size());
    }
}
