package ai.traceai.examples.quarkus;

import dev.langchain4j.model.chat.listener.ChatModelErrorContext;
import dev.langchain4j.observability.api.event.AiServiceErrorEvent;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.quarkiverse.langchain4j.runtime.listeners.ChatModelSpanContributor;
import io.quarkus.arc.Unremovable;
import io.quarkus.opentelemetry.runtime.AutoConfiguredOpenTelemetrySdkBuilderCustomizer;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdkBuilder;
import jakarta.annotation.Priority;
import jakarta.enterprise.event.Observes;
import jakarta.inject.Singleton;
import org.eclipse.microprofile.config.inject.ConfigProperty;

/**
 * Future AGI defaults for the spans Quarkus LangChain4j already creates. Application code: copy this file (and
 * {@link ToolErrorStatus}) into your application. Nothing here creates spans.
 *
 * <ol>
 * <li>Failures become {@code ERROR}. Quarkus LangChain4j records the exception on the AI service span and on the
 * model span but leaves the span status {@code UNSET}, so a failed call would look successful.</li>
 * <li>The status description stays empty unless {@code fi.export-error-details=true}
 * (env {@code FI_EXPORT_ERROR_DETAILS=true}). Exception messages often contain the prompt, a tool argument or the
 * provider's error body. The exception events themselves are dropped by {@code quarkus.otel.span.event.count.limit=0}
 * in application.properties.</li>
 * <li>{@link DropHostName} removes {@code host.name}, which Quarkus always adds to the resource.</li>
 * </ol>
 */
@Singleton
public class FutureAgiTracingDefaults implements ChatModelSpanContributor {

    static final String AI_SERVICE_SPAN_PREFIX = "langchain4j.aiservices.";

    private final boolean exportErrorDetails;

    public FutureAgiTracingDefaults(
            @ConfigProperty(name = "fi.export-error-details", defaultValue = "false") boolean exportErrorDetails) {
        this.exportErrorDetails = exportErrorDetails;
    }

    /** The model span ({@code completion <model>}) of a failed model call. */
    @Override
    public void onError(ChatModelErrorContext errorContext, Span currentSpan) {
        markError(currentSpan, errorContext.error(), exportErrorDetails);
    }

    /**
     * Fired by Quarkus LangChain4j for any failure of an AI service method (model error, tool error that propagates,
     * output parsing). It runs while the {@code langchain4j.aiservices.*} span is still the current span.
     */
    void onAiServiceError(@Observes AiServiceErrorEvent event) {
        Span current = Span.current();
        if (current instanceof ReadableSpan readable && readable.getName().startsWith(AI_SERVICE_SPAN_PREFIX)) {
            markError(current, event.error(), exportErrorDetails);
        }
    }

    static void markError(Span span, Throwable error, boolean withDetails) {
        String description = withDetails && error != null && error.getMessage() != null ? error.getMessage() : "";
        span.setStatus(StatusCode.ERROR, description);
    }

    /**
     * Quarkus puts {@code host.name} on the resource after {@code quarkus.otel.resource.attributes} and after the
     * resource-key filter, so configuration cannot remove it. Quarkus applies these customizers in descending bean
     * priority; the lowest priority makes this one run after Quarkus' own resource customizer.
     */
    @Singleton
    @Unremovable
    @Priority(Integer.MIN_VALUE)
    public static class DropHostName implements AutoConfiguredOpenTelemetrySdkBuilderCustomizer {

        static final AttributeKey<String> HOST_NAME = AttributeKey.stringKey("host.name");

        @Override
        public void customize(AutoConfiguredOpenTelemetrySdkBuilder builder) {
            builder.addResourceCustomizer((resource, config) -> withoutHostName(resource));
        }

        static Resource withoutHostName(Resource resource) {
            return resource.toBuilder().removeIf(HOST_NAME::equals).build();
        }
    }
}
