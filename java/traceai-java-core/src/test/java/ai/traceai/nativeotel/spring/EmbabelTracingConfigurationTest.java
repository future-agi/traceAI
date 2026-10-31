package ai.traceai.nativeotel.spring;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.MapPropertySource;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/** Configuration remains loadable at core's Spring 6 / OpenTelemetry 1.35 compile floor. */
class EmbabelTracingConfigurationTest {
    private static final String CONTENT = "embabel.agent.platform.observability.capture-message-content";

    @Test
    void importAddsPrivateDefaultsAndRoutingButNoTelemetryProducer() {
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
            context.getEnvironment().getPropertySources().addFirst(new MapPropertySource("test", Map.of(
                    "fi.project-name", "fixture-project")));
            context.register(EmbabelTracingConfiguration.class, UserResource.class);
            context.refresh();
            Resource resource = context.getBean(Resource.class);
            assertEquals("fixture-project", resource.getAttribute(AttributeKey.stringKey("project_name")));
            assertEquals("observe", resource.getAttribute(AttributeKey.stringKey("project_type")));
            assertEquals("native-service", resource.getAttribute(AttributeKey.stringKey("service.name")));
            assertEquals("https://example.invalid/schema", resource.getSchemaUrl());
            assertFalse(context.getEnvironment().getProperty(CONTENT, Boolean.class, true));
            assertTrue(context.getBeansOfType(SdkTracerProvider.class).isEmpty());
            assertTrue(context.getBeansOfType(SpanExporter.class).isEmpty());
        }
    }

    @Test
    void anExplicitApplicationSettingWinsOverPrivateDefaults() {
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
            context.getEnvironment().getPropertySources().addFirst(new MapPropertySource("test", Map.of(
                    "fi.project-name", "fixture-project", CONTENT, true)));
            context.register(EmbabelTracingConfiguration.class, UserResource.class);
            context.refresh();
            assertTrue(context.getEnvironment().getProperty(CONTENT, Boolean.class, false));
        }
    }

    @Test
    void nothingChangesUnlessTheConfigurationIsImported() {
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext(UserResource.class)) {
            assertNull(context.getBean(Resource.class).getAttribute(AttributeKey.stringKey("project_name")));
            assertNull(context.getEnvironment().getProperty(CONTENT));
        }
    }

    @Configuration(proxyBeanMethods = false)
    static class UserResource {
        @Bean
        Resource resource() {
            return Resource.create(Attributes.of(AttributeKey.stringKey("service.name"), "native-service"),
                    "https://example.invalid/schema");
        }
    }
}
