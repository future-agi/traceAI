package ai.traceai.auto;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.*;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class ResourceIdentityTest {
    @Test void fiProjectNeverReplacesRegisteredApplicationResource() {
        GlobalOpenTelemetry.resetForTest();
        Resource app = Resource.create(Attributes.builder().put("service.name", "application-service").put("service.namespace", "application-space")
            .put("service.instance.id", "instance-1").put("cloud.region", "region-1").put("custom.identity", "custom").build(), "https://example.invalid/schema");
        try (var provider = SdkTracerProvider.builder().setResource(app).build()) {
            OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
            var config = AutoConfig.resolve(Map.of("project_name", "routing-project", "OTEL_SERVICE_NAME", "agent-fallback"), Map.of());
            var resource = TraceAIAuto.resource(config);
            app.getAttributes().forEach((key, value) -> assertEquals(value, resource.getAttribute(key), key.toString()));
            assertEquals("routing-project", resource.getAttribute(AttributeKey.stringKey("project_name")));
            assertEquals("https://example.invalid/schema", resource.getSchemaUrl());
            assertEquals("application-service", app.getAttribute(AttributeKey.stringKey("service.name")));
        } finally { GlobalOpenTelemetry.resetForTest(); }
    }
    @Test void lateNonGlobalParentResourceSurvivesAsyncWireExportWithoutChangingHostView() {
        GlobalOpenTelemetry.resetForTest();
        var config = AutoConfig.resolve(Map.of("project_name", "routing-project"), Map.of());
        Resource initial = TraceAIAuto.resource(config);
        Resource identity = Resource.create(Attributes.builder().put("service.name", "late-local-service").put("service.namespace", "local-space")
            .put("custom.identity", "local").build(), "https://example.invalid/local-schema");
        var wireMemory = io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter.create();
        var hostMemory = io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter.create();
        var batch = io.opentelemetry.sdk.trace.export.BatchSpanProcessor.builder(wireMemory).setScheduleDelay(java.time.Duration.ofHours(1)).build();
        try (var wire = SdkTracerProvider.builder().setResource(initial).addSpanProcessor(new WireResourceProcessor(batch)).build();
             var host = SdkTracerProvider.builder().setResource(identity).addSpanProcessor(io.opentelemetry.sdk.trace.export.SimpleSpanProcessor.create(hostMemory)).build()) {
            var runtime = new TraceAIAuto.RuntimeState(config, wire, wire.get("wire"), new OtlpJsonExporter(config), batch, initial);
            var parent = host.get("host").spanBuilder("parent").startSpan();
            OtelBridge.Snapshot context;
            try (var scope = parent.makeCurrent()) { context = OtelBridge.snapshot(); }
            assertEquals(identity, context.resource());
            var request = AdversarialTest.request("http://127.0.0.1/v1/chat/completions", Map.of("messages", List.of()));
            var match = WireCore.match(request.method, request.url);
            var capture = new WireCapture(request, request.headers, match, context, 4194304, runtime); capture.endEpochNanos = context.startEpochNanos() + 1;
            TraceAIAuto.emit(capture, WireCore.build(match, request, AdversarialTest.response("application/json", "{}")));
            wire.forceFlush().join(5, java.util.concurrent.TimeUnit.SECONDS);
            var exported = wireMemory.getFinishedSpanItems().get(0);
            identity.getAttributes().forEach((key, value) -> assertEquals(value, exported.getResource().getAttribute(key)));
            assertEquals(identity.getSchemaUrl(), exported.getResource().getSchemaUrl());
            assertEquals("routing-project", exported.getResource().getAttribute(AttributeKey.stringKey("project_name")));
            assertEquals(parent.getSpanContext().getSpanId(), exported.getParentSpanId());
            parent.end(); assertEquals(1, hostMemory.getFinishedSpanItems().size());
            assertEquals(identity, hostMemory.getFinishedSpanItems().get(0).getResource());
            assertNull(identity.getAttribute(AttributeKey.stringKey("project_name")));
        } finally { GlobalOpenTelemetry.resetForTest(); }
    }
    @Test void ownedResourceUsesOtelEnvironmentAndNeverProjectFallback() {
        GlobalOpenTelemetry.resetForTest();
        var config = AutoConfig.resolve(Map.of("project_name", "routing-project"), Map.of("OTEL_RESOURCE_ATTRIBUTES", "service.name=resource-service,service.namespace=space,custom.identity=abc%20xyz"));
        var resource = TraceAIAuto.resource(config);
        assertEquals("resource-service", resource.getAttribute(AttributeKey.stringKey("service.name")));
        assertEquals("space", resource.getAttribute(AttributeKey.stringKey("service.namespace")));
        assertEquals("abc xyz", resource.getAttribute(AttributeKey.stringKey("custom.identity")));
        config = AutoConfig.resolve(Map.of("project_name", "routing-project"), Map.of());
        assertNotEquals("routing-project", TraceAIAuto.resource(config).getAttribute(AttributeKey.stringKey("service.name")));
        GlobalOpenTelemetry.resetForTest();
    }
}
