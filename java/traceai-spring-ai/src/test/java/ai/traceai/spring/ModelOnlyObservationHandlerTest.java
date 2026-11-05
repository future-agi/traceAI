package ai.traceai.spring;

import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationRegistry;
import io.micrometer.tracing.handler.DefaultTracingObservationHandler;
import io.micrometer.tracing.otel.bridge.OtelCurrentTraceContext;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.URL;
import java.net.URLClassLoader;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.chat.prompt.Prompt;
import static org.assertj.core.api.Assertions.assertThat;

class ModelOnlyObservationHandlerTest {
    @Test void handlerWorksWithoutOptionalChatClientClasses() throws Exception {
        String handlerName = SpringAITracingObservationHandler.class.getName();
        URL classes = SpringAITracingObservationHandler.class.getProtectionDomain().getCodeSource().getLocation();
        // Load the handler and its nested helper afresh, with the optional client artifact absent.
        try (var loader = new URLClassLoader(new URL[] { classes }, getClass().getClassLoader()) {
            @Override protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
                if (name.startsWith("org.springframework.ai.chat.client.")) throw new ClassNotFoundException(name);
                if (!name.startsWith(handlerName)) return super.loadClass(name, resolve);
                synchronized (getClassLoadingLock(name)) {
                    Class<?> loaded = findLoadedClass(name);
                    if (loaded == null) loaded = findClass(name);
                    if (resolve) resolveClass(loaded);
                    return loaded;
                }
            }
        }) {
            var exporter = InMemorySpanExporter.create();
            try (var sdk = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
                var tracer = new OtelTracer(sdk.get("model-only"), new OtelCurrentTraceContext(), event -> {});
                var handler = (DefaultTracingObservationHandler) loader.loadClass(handlerName)
                    .getConstructor(OtelTracer.class, boolean.class).newInstance(tracer, true);
                var registry = ObservationRegistry.create();
                registry.observationConfig().observationHandler(handler);
                Observation.start("generic", registry).stop();
                Observation.start("incomplete", ModelOnlyObservationHandlerTest::chat, registry).stop();
                Observation.start("failed", ModelOnlyObservationHandlerTest::chat, registry)
                    .error(new IllegalStateException("synthetic-private-error")).stop();
                var completed = chat();
                completed.setResponse(SpanTestSupport.response("answer"));
                Observation.start("completed", () -> completed, registry).stop();
                var spans = exporter.getFinishedSpanItems();
                assertThat(spans).extracting(span -> span.getStatus().getStatusCode())
                    .containsExactly(StatusCode.OK, StatusCode.UNSET, StatusCode.ERROR, StatusCode.OK);
                assertThat(spans.toString()).doesNotContain("synthetic-private-error");
            }
        }
    }

    private static ChatModelObservationContext chat() {
        return ChatModelObservationContext.builder().prompt(new Prompt("input")).provider("synthetic").build();
    }
}
