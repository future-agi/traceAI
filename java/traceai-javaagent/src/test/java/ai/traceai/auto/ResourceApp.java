package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;

/** Same app runs with and without premain; its SDK is configured after the agent's startup. */
public final class ResourceApp {
    static void run(String[] args) throws Exception {
        var memory = InMemorySpanExporter.create();
        var identity = Resource.create(Attributes.builder().put("service.name", "resource-app-service").put("service.namespace", "resource-app-space")
            .put("service.instance.id", "resource-app-instance").put("custom.identity", "application-owned").build());
        try (var provider = SdkTracerProvider.builder().setResource(identity).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
            var parent = provider.get("resource-app").spanBuilder("resource-parent").startSpan(); parent.setAttribute("gen_ai.span.kind", "CHAIN");
            try (var scope = parent.makeCurrent()) { PlainApp.call(args); } finally { parent.end(); }
            if (memory.getFinishedSpanItems().size() != 1) throw new AssertionError("Host exporter changed");
            var original = memory.getFinishedSpanItems().get(0);
            if (!identity.equals(original.getResource())) throw new AssertionError("Host resource changed");
            System.out.println("HOST_RESOURCE=unchanged");
            try { Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("flush").invoke(null); } catch (ClassNotFoundException ignored) { }
        }
    }
}
