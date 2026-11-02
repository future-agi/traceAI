package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.micrometer.observation.Observation;
import io.micrometer.tracing.Span;
import io.micrometer.tracing.handler.DefaultTracingObservationHandler;
import io.micrometer.tracing.otel.bridge.OtelSpan;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.StatusCode;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.embedding.observation.EmbeddingModelObservationContext;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;

/**
 * Native Micrometer OpenTelemetry handler with Spring AI error privacy and completion-aware status.
 * Register this <em>instead of</em> the generic {@link DefaultTracingObservationHandler}, alongside
 * {@link SpringAIObservationFilter}. It uses the application's existing tracer and creates exactly
 * the same native spans as the generic handler. Other observation types retain generic behavior.
 */
public final class SpringAITracingObservationHandler extends DefaultTracingObservationHandler {
    private final boolean hideErrorDetails;

    /**
     * Uses the application's OpenTelemetry bridge and {@code FI_HIDE_ERROR_DETAILS} policy.
     * @param tracer the existing Micrometer OpenTelemetry tracer
     */
    public SpringAITracingObservationHandler(OtelTracer tracer) {
        this(tracer, TraceConfig.builder().build().isHideErrorDetails());
    }

    /**
     * Uses an explicit error-privacy policy, independent of prompt/completion capture.
     * @param tracer the existing Micrometer OpenTelemetry tracer
     * @param hideErrorDetails omit exception messages, stacks and status descriptions for Spring AI
     */
    public SpringAITracingObservationHandler(OtelTracer tracer, boolean hideErrorDetails) {
        super(tracer);
        this.hideErrorDetails = hideErrorDetails;
    }

    @Override
    public void onError(Observation.Context context) {
        if (!hideErrorDetails || !isSpringAI(context)) {
            super.onError(context);
            return;
        }
        Throwable error = context.getError();
        if (error != null) {
            // Never hand the original throwable to Span.error/recordException: both record its stack.
            // Do not mutate the context or throwable; metrics and application code still see the cause.
            var span = OtelSpan.toOtel(getRequiredSpan(context));
            String type = error.getClass().getName();
            span.setAttribute(SemanticConventions.ERROR_TYPE, type);
            span.setStatus(StatusCode.ERROR);
            span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.type"), type));
        }
    }

    @Override
    public void tagSpan(Observation.Context context, Span span) {
        if (!hideErrorDetails || !isSpringAI(context)) {
            super.tagSpan(context, span);
            return;
        }
        for (var value : context.getAllKeyValues()) {
            String key = value.getKey();
            // Micrometer treats the special "error" tag as another Span.error(message) call.
            if ("error".equalsIgnoreCase(key)) {
                OtelSpan.toOtel(span).setStatus(StatusCode.ERROR);
            } else if (!"exception.message".equals(key) && !"exception.stacktrace".equals(key)
                    && !"error.message".equals(key)) {
                span.tag(key, value.getValue());
            }
        }
    }

    @Override
    public void endSpan(Observation.Context context, Span span) {
        if (context instanceof ChatModelObservationContext chat && chat.getResponse() == null) {
            // Spring AI's stream aggregator supplies the response only on completion. Cancellation
            // (including downstream timeout) stops the observation without onError or a response.
            // Bypass OtelSpan.end's automatic OK, preserving UNSET (unknown) or an existing ERROR.
            // An absent response alone cannot distinguish cancellation from an empty provider reply,
            // so do not invent a TimeoutException or claim a provider failure.
            getTracingContext(context).close();
            OtelSpan.toOtel(span).end();
        } else {
            super.endSpan(context, span);
        }
    }

    private static boolean isSpringAI(Observation.Context context) {
        return context instanceof ChatModelObservationContext
            || context instanceof EmbeddingModelObservationContext
            || context instanceof ToolCallingObservationContext;
    }
}
