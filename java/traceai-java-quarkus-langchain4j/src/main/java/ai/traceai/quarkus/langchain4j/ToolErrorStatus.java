package ai.traceai.quarkus.langchain4j;

import io.opentelemetry.api.trace.Span;
import io.quarkiverse.langchain4j.runtime.tool.ToolExecutionErrorContext;
import io.quarkiverse.langchain4j.runtime.tool.ToolSpanContributor;
import jakarta.inject.Singleton;
import org.eclipse.microprofile.config.inject.ConfigProperty;

/**
 * Marks the {@code langchain4j.tools.<name>} span of a tool that threw as {@code ERROR}. Quarkus LangChain4j records
 * the exception but leaves the status {@code UNSET}. Requires Quarkus LangChain4j 1.12.0 or newer.
 */
@Singleton
public class ToolErrorStatus implements ToolSpanContributor {

    private final boolean exportErrorDetails;

    public ToolErrorStatus(@ConfigProperty(name = "fi.export-error-details", defaultValue = "false") boolean exportErrorDetails) {
        this.exportErrorDetails = exportErrorDetails;
    }

    @Override
    public void onError(ToolExecutionErrorContext context, Span span) {
        FutureAgiTracingDefaults.markError(span, context.error(), exportErrorDetails);
    }
}
