package ai.traceai.nativeotel.spring;

import ai.traceai.privacy.ErrorDetailRedactingSpanExporter;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.springframework.beans.factory.config.BeanFactoryPostProcessor;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.EnvironmentAware;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.ConfigurableEnvironment;
import org.springframework.core.env.Environment;
import org.springframework.core.env.MapPropertySource;

import java.util.Map;
import java.util.Set;

/**
 * Routing and privacy for Embabel's native telemetry, enabled only by an explicit {@code @Import}.
 *
 * <p>Creates no SDK, provider, processor, exporter or spans. The application keeps Embabel's
 * observability starter and Spring Boot's OTLP exporter. Set {@code fi.project-name} (or
 * {@code FI_PROJECT_NAME}) to route to an Observe project. An existing Resource project name takes
 * precedence over Boot's resource-attributes property, but an explicit FI project overrides both.
 * Other resource attributes, including service identity, are preserved.
 *
 * <p>Content and HTTP details default to off, before Embabel's properties are bound. Explicit
 * application settings win. Error text follows Embabel's {@code capture-message-content} setting;
 * exception stack traces are always removed. Only Spring-managed exporters are covered, and this
 * is not a scrubber for arbitrary application attributes, event names, logs or metrics.
 *
 * <p>Use this instead of the TraceAI Spring Boot starter, not alongside it. Spring is supplied by
 * the application. Tested with Embabel 1.5.3/1.5.4 and Spring Boot 4.1.1.
 */
@Configuration(proxyBeanMethods = false)
public class EmbabelTracingConfiguration {
    private static final String PREFIX = "embabel.agent.platform.observability.";
    private static final AttributeKey<String> PROJECT_NAME = AttributeKey.stringKey("project_name");
    private static final Set<String> ERROR_MESSAGE_KEYS = Set.of(
            "error.message", "embabel.llm.error.message", "embabel.tool.error.message");

    @Bean
    static BeanFactoryPostProcessor embabelPrivateContentDefaults(ConfigurableEnvironment environment) {
        return beanFactory -> environment.getPropertySources().addLast(new MapPropertySource(
                "traceaiEmbabelPrivacyDefaults", Map.of(
                        PREFIX + "capture-message-content", false,
                        PREFIX + "trace-http-details", false)));
    }

    @Bean
    static BeanPostProcessor embabelNativeTelemetryPostProcessor() {
        return new NativeTelemetryPostProcessor();
    }

    private static final class NativeTelemetryPostProcessor implements BeanPostProcessor, EnvironmentAware {
        private Environment environment;
        private boolean captureContent;

        @Override
        public void setEnvironment(Environment environment) {
            this.environment = environment;
            captureContent = environment.getProperty(PREFIX + "capture-message-content", Boolean.class, false);
        }

        @Override
        public Object postProcessAfterInitialization(Object bean, String beanName) {
            if (bean instanceof Resource resource) {
                String project = environment.getProperty("fi.project-name");
                if (project == null) {
                    project = environment.getProperty("FI_PROJECT_NAME");
                }
                if (project == null) {
                    project = resource.getAttribute(PROJECT_NAME);
                }
                if (project == null) {
                    project = environment.getProperty("management.opentelemetry.resource-attributes.project_name");
                }
                if (project == null || project.isBlank()) {
                    throw new IllegalStateException("Set fi.project-name (FI_PROJECT_NAME) to an Observe project");
                }
                return resource.merge(Resource.create(Attributes.builder()
                        .put(PROJECT_NAME, project).put("project_type", "observe").build()));
            }
            if (bean instanceof SpanExporter exporter) {
                return ErrorDetailRedactingSpanExporter.wrap(exporter, ERROR_MESSAGE_KEYS, captureContent);
            }
            return bean;
        }
    }
}
