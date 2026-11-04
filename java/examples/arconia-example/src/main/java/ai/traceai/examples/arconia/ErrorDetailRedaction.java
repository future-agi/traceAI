package ai.traceai.examples.arconia;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.DelegatingSpanData;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.data.StatusData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.EnvironmentAware;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.Environment;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Set;

/**
 * Keeps raw error text out of what leaves the JVM.
 *
 * <p>Arconia (through Micrometer Tracing) records an unhandled exception on the server span as an
 * {@code exception} event that carries {@code exception.message} and the whole {@code exception.stacktrace},
 * and copies the message into the span status description. Messages often contain user input or
 * identifiers. This class wraps every {@link SpanExporter} bean and removes those three things before export.
 * The exception class name ({@code exception.type}), the span status (ERROR) and the event itself are kept.
 *
 * <p>Opt in to the raw details with {@code FI_EXPORT_ERROR_DETAILS=true} (property {@code fi.export-error-details}).
 * This is application code, not an Arconia feature: Arconia has no switch for it.
 */
@Configuration(proxyBeanMethods = false)
public class ErrorDetailRedaction {

    static final Set<String> RAW_ERROR_KEYS = Set.of("exception.message", "exception.stacktrace");

    @Bean
    static BeanPostProcessor redactErrorDetailsPostProcessor() {
        return new RedactingPostProcessor();
    }

    static final class RedactingPostProcessor implements BeanPostProcessor, EnvironmentAware {
        private boolean exportRawDetails;

        @Override
        public void setEnvironment(Environment environment) {
            this.exportRawDetails = environment.getProperty("fi.export-error-details", Boolean.class, false);
        }

        @Override
        public Object postProcessAfterInitialization(Object bean, String beanName) {
            if (!exportRawDetails && bean instanceof SpanExporter exporter && !(bean instanceof RedactingSpanExporter)) {
                return new RedactingSpanExporter(exporter);
            }
            return bean;
        }
    }

    static final class RedactingSpanExporter implements SpanExporter {
        private final SpanExporter delegate;

        RedactingSpanExporter(SpanExporter delegate) {
            this.delegate = delegate;
        }

        @Override
        public CompletableResultCode export(Collection<SpanData> spans) {
            List<SpanData> out = new ArrayList<>(spans.size());
            for (SpanData span : spans) {
                out.add(redact(span));
            }
            return delegate.export(out);
        }

        @Override
        public CompletableResultCode flush() {
            return delegate.flush();
        }

        @Override
        public CompletableResultCode shutdown() {
            return delegate.shutdown();
        }

        static SpanData redact(SpanData span) {
            boolean hasRaw = span.getEvents().stream().anyMatch(
                    e -> e.getAttributes().asMap().keySet().stream().anyMatch(k -> RAW_ERROR_KEYS.contains(k.getKey())));
            boolean hasStatusText = !span.getStatus().getDescription().isEmpty();
            if (!hasRaw && !hasStatusText) {
                return span;
            }
            List<EventData> events = new ArrayList<>(span.getEvents().size());
            for (EventData e : span.getEvents()) {
                var attrs = e.getAttributes().toBuilder()
                        .removeIf(k -> RAW_ERROR_KEYS.contains(((AttributeKey<?>) k).getKey()))
                        .build();
                events.add(EventData.create(e.getEpochNanos(), e.getName(), attrs, attrs.size()));
            }
            StatusData status = StatusData.create(span.getStatus().getStatusCode(), "");
            return new DelegatingSpanData(span) {
                @Override
                public List<EventData> getEvents() {
                    return events;
                }

                @Override
                public StatusData getStatus() {
                    return status;
                }
            };
        }
    }
}
