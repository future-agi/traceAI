package ai.traceai.privacy;

import ai.traceai.privacy.spring.ErrorDetailRedactionConfiguration;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.core.env.MapPropertySource;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class ErrorDetailRedactionConfigurationTest {
    @Test
    void importingThePolicyWrapsEveryExporterButCreatesNoTelemetry() {
        try (var context = new AnnotationConfigApplicationContext()) {
            context.getEnvironment().getPropertySources().addFirst(new MapPropertySource("synthetic",
                    Map.of("fi.export-error-details", "false")));
            context.registerBean("http", SpanExporter.class, ErrorDetailRedactingSpanExporterTest.RecordingExporter::new);
            context.registerBean("other", SpanExporter.class, ErrorDetailRedactingSpanExporterTest.RecordingExporter::new);
            context.register(ErrorDetailRedactionConfiguration.class);
            context.refresh();
            assertEquals(2, context.getBeansOfType(SpanExporter.class).size());
            context.getBeansOfType(SpanExporter.class).values().forEach(exporter ->
                    assertInstanceOf(ErrorDetailRedactingSpanExporter.class, exporter));
            assertTrue(context.getBeansOfType(SdkTracerProvider.class).isEmpty());
            assertTrue(context.getBeansOfType(SpanProcessor.class).isEmpty());
        }
    }

    @Test
    void absentPropertyDefaultsToRedaction() {
        try (var context = new AnnotationConfigApplicationContext()) {
            context.getEnvironment().getPropertySources().remove("systemEnvironment");
            context.getEnvironment().getPropertySources().remove("systemProperties");
            context.registerBean("exporter", SpanExporter.class, ErrorDetailRedactingSpanExporterTest.RecordingExporter::new);
            context.register(ErrorDetailRedactionConfiguration.class);
            context.refresh();
            assertInstanceOf(ErrorDetailRedactingSpanExporter.class, context.getBean(SpanExporter.class));
        }
    }

    @Test
    void explicitOptInPreservesTheOriginalExporter() {
        var sink = new ErrorDetailRedactingSpanExporterTest.RecordingExporter();
        try (var context = new AnnotationConfigApplicationContext()) {
            context.getEnvironment().getPropertySources().addFirst(new MapPropertySource("synthetic",
                    Map.of("fi.export-error-details", "true")));
            context.registerBean("exporter", SpanExporter.class, () -> sink);
            context.register(ErrorDetailRedactionConfiguration.class);
            context.refresh();
            assertSame(sink, context.getBean(SpanExporter.class));
        }
    }

    @Test
    void anApplicationWithoutTheImportIsUnchanged() {
        var sink = new ErrorDetailRedactingSpanExporterTest.RecordingExporter();
        try (var context = new AnnotationConfigApplicationContext()) {
            context.registerBean("exporter", SpanExporter.class, () -> sink);
            context.refresh();
            assertSame(sink, context.getBean(SpanExporter.class));
        }
    }
}
