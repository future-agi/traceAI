package ai.traceai.examples.arconia;

import ai.traceai.privacy.spring.ErrorDetailRedactionConfiguration;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.context.annotation.Import;

import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/** Uses only the library import: no component scan, copied helper, controller or TraceAI SDK setup. */
class NativePrivacyImportTest {
    @EnableAutoConfiguration
    @Import(ErrorDetailRedactionConfiguration.class)
    static class NativeApplication {
    }

    private List<OtlpRecorder.Post> run(boolean rawDetails) throws Exception {
        try (var collector = new OtlpRecorder()) {
            SpringApplication app = new SpringApplication(NativeApplication.class);
            app.setWebApplicationType(WebApplicationType.NONE);
            try (var context = app.run(
                    "--FI_BASE_URL=" + collector.baseUrl(),
                    "--FI_API_KEY=synthetic-key", "--FI_SECRET_KEY=synthetic-secret", "--FI_PROJECT_NAME=privacy-test",
                    "--fi.export-error-details=" + rawDetails)) {
                assertEquals(1, context.getBeansOfType(SdkTracerProvider.class).size(), "native SDK only");
                var tracer = context.getBean(OpenTelemetry.class).getTracer("native-privacy-test");
                var span = tracer.spanBuilder("native-error").startSpan();
                span.setAttribute("exception.message", "PRIVATE_ATTRIBUTE_ERROR");
                span.setAttribute("exception.stacktrace", "PRIVATE_ATTRIBUTE_STACK");
                span.recordException(new IllegalStateException("PRIVATE_NATIVE_ERROR"));
                span.setStatus(StatusCode.ERROR, "PRIVATE_NATIVE_STATUS");
                span.end();
            }
            return collector.posts();
        }
    }

    @Test
    void libraryImportAloneRedactsBeforeHttpExportAndRetainsErrorType() throws Exception {
        var posts = run(false);
        assertEquals(1, posts.size());
        var post = posts.get(0);
        String wire = new String(post.body(), StandardCharsets.ISO_8859_1);
        assertFalse(wire.contains("PRIVATE_NATIVE_ERROR"));
        assertFalse(wire.contains("PRIVATE_NATIVE_STATUS"));
        assertFalse(wire.contains("PRIVATE_ATTRIBUTE_ERROR"));
        assertFalse(wire.contains("PRIVATE_ATTRIBUTE_STACK"));
        assertFalse(wire.contains("exception.stacktrace"));
        var spans = post.decode().getResourceSpansList().stream()
                .flatMap(resource -> resource.getScopeSpansList().stream())
                .flatMap(scope -> scope.getSpansList().stream()).toList();
        assertEquals(1, spans.size(), "no duplicate spans");
        assertEquals("native-error", spans.get(0).getName());
        assertEquals(io.opentelemetry.proto.trace.v1.Status.StatusCode.STATUS_CODE_ERROR,
                spans.get(0).getStatus().getCode());
        assertEquals("", spans.get(0).getStatus().getMessage());
        assertTrue(spans.get(0).getEvents(0).getAttributesList().stream().anyMatch(attribute ->
                attribute.getKey().equals("exception.type") &&
                attribute.getValue().getStringValue().equals("java.lang.IllegalStateException")));
    }

    @Test
    void sameOptInStillExportsRawErrorDetails() throws Exception {
        var posts = run(true);
        assertFalse(posts.isEmpty());
        String wire = new String(posts.get(0).body(), StandardCharsets.ISO_8859_1);
        assertTrue(wire.contains("PRIVATE_NATIVE_ERROR"));
        assertTrue(wire.contains("PRIVATE_NATIVE_STATUS"));
        assertTrue(wire.contains("exception.stacktrace"));
    }
}
