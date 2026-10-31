package ai.traceai.privacy.spring;

import ai.traceai.privacy.ErrorDetailRedactingSpanExporter;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.EnvironmentAware;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.Environment;

/**
 * Explicitly imported privacy policy for Spring applications with framework-owned telemetry.
 *
 * <p>Add {@code @Import(ErrorDetailRedactionConfiguration.class)} to your configuration. Every
 * {@link SpanExporter} bean is wrapped before it is injected into the framework's span processor.
 * No provider, exporter or processor is created. Exporters constructed outside Spring are not
 * covered; wrap those with {@link ErrorDetailRedactingSpanExporter#wrap(SpanExporter)} instead.
 *
 * <p>Raw error details are off by default. Set {@code fi.export-error-details=true} (environment
 * variable {@code FI_EXPORT_ERROR_DETAILS=true}) to opt in. This policy is independent of FITracer's
 * {@code FI_HIDE_ERROR_DETAILS} option. Spring is supplied by the application, not by Java core.
 */
@Configuration(proxyBeanMethods = false)
public class ErrorDetailRedactionConfiguration {
    @Bean
    static BeanPostProcessor redactErrorDetailsPostProcessor() {
        return new RedactingPostProcessor();
    }

    private static final class RedactingPostProcessor implements BeanPostProcessor, EnvironmentAware {
        private boolean exportRawDetails;

        @Override
        public void setEnvironment(Environment environment) {
            exportRawDetails = environment.getProperty("fi.export-error-details", Boolean.class, false);
        }

        @Override
        public Object postProcessAfterInitialization(Object bean, String beanName) {
            if (!exportRawDetails && bean instanceof SpanExporter exporter) {
                return ErrorDetailRedactingSpanExporter.wrap(exporter);
            }
            return bean;
        }
    }
}
