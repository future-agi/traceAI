package ai.traceai.auto;

import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.samplers.Sampler;

/** An application with its own SDK that samples nothing makes an AI call inside one of its spans. */
public final class UnsampledApp {
    static void run(String[] args) throws Exception {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setSampler(Sampler.alwaysOff()).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.tracerBuilder("unsampled-application-scope").build().spanBuilder("application-unsampled-span").startSpan();
            System.out.println("APP_SPAN=" + span.getSpanContext().getSpanId());
            System.out.println("APP_SPAN_SAMPLED=" + span.getSpanContext().isSampled());
            try (var scope = span.makeCurrent()) { PlainApp.call(args); } finally { span.end(); }
            if (!memory.getFinishedSpanItems().isEmpty()) throw new AssertionError("The application's own provider recorded a span it does not sample");
            System.out.println("APP_SPAN_STAYED_UNSAMPLED=true");
        }
    }
}
