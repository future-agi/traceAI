package ai.traceai;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanBuilder;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

/**
 * Main tracer class for TraceAI instrumentation.
 * Provides methods to create and manage spans for AI operations.
 */
public class FITracer {

    private final Tracer tracer;
    private final TraceConfig config;
    private final Gson gson;

    /**
     * Creates a new FITracer with the given OpenTelemetry tracer and configuration.
     * @param tracer the OpenTelemetry tracer
     * @param config the trace configuration
     */
    public FITracer(Tracer tracer, TraceConfig config) {
        this.tracer = tracer;
        this.config = config != null ? config : TraceConfig.builder().build();
        this.gson = new GsonBuilder()
            .serializeNulls()
            .disableHtmlEscaping()
            .create();
    }

    /**
     * Creates a new FITracer with the given OpenTelemetry tracer and default configuration.
     * @param tracer the OpenTelemetry tracer
     */
    public FITracer(Tracer tracer) {
        this(tracer, null);
    }

    /**
     * Starts a new span with the given name and span kind.
     * @param name the span name
     * @param kind the FI span kind
     * @return the started span
     */
    public Span startSpan(String name, FISpanKind kind) {
        SpanBuilder builder = tracer.spanBuilder(name)
            .setSpanKind(SpanKind.INTERNAL);

        Span span = builder.startSpan();
        span.setAttribute(SemanticConventions.FI_SPAN_KIND, kind.getValue());

        return span;
    }

    /**
     * Starts a new span with the given name, span kind, and parent context.
     * @param name the span name
     * @param kind the FI span kind
     * @param parent the parent context
     * @return the started span
     */
    public Span startSpan(String name, FISpanKind kind, Context parent) {
        SpanBuilder builder = tracer.spanBuilder(name)
            .setSpanKind(SpanKind.INTERNAL)
            .setParent(parent);

        Span span = builder.startSpan();
        span.setAttribute(SemanticConventions.FI_SPAN_KIND, kind.getValue());

        return span;
    }

    /**
     * Sets the input value on a span if not configured to hide inputs.
     * @param span the span
     * @param value the input value
     */
    public void setInputValue(Span span, String value) {
        if (!config.isHideInputs() && value != null) {
            span.setAttribute(SemanticConventions.INPUT_VALUE, truncateIfNeeded(value));
        }
    }

    /**
     * Sets the output value on a span if not configured to hide outputs.
     * @param span the span
     * @param value the output value
     */
    public void setOutputValue(Span span, String value) {
        if (!config.isHideOutputs() && value != null) {
            span.setAttribute(SemanticConventions.OUTPUT_VALUE, truncateIfNeeded(value));
        }
    }

    /**
     * Sets the raw input JSON on a span if not configured to hide inputs.
     * @param span the span
     * @param value the raw input object (will be serialized to JSON)
     */
    public void setRawInput(Span span, Object value) {
        if (!config.isHideInputs() && value != null) {
            String json = toJson(value);
            span.setAttribute(SemanticConventions.RAW_INPUT, truncateIfNeeded(json));
        }
    }

    /**
     * Sets the raw output JSON on a span if not configured to hide outputs.
     * @param span the span
     * @param value the raw output object (will be serialized to JSON)
     */
    public void setRawOutput(Span span, Object value) {
        if (!config.isHideOutputs() && value != null) {
            String json = toJson(value);
            span.setAttribute(SemanticConventions.RAW_OUTPUT, truncateIfNeeded(json));
        }
    }

    /**
     * Sets token count attributes on a span.
     * @param span the span
     * @param prompt the prompt token count
     * @param completion the completion token count
     * @param total the total token count
     */
    public void setTokenCounts(Span span, int prompt, int completion, int total) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, prompt);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, completion);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total);
    }

    /**
     * Sets token count attributes on a span using long values.
     * @param span the span
     * @param prompt the prompt token count
     * @param completion the completion token count
     * @param total the total token count
     */
    public void setTokenCounts(Span span, long prompt, long completion, long total) {
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, prompt);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, completion);
        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total);
    }

    /**
     * Sets input messages on a span as a JSON blob.
     * Each message should be a Map with "role" and "content" keys.
     * @param span the span
     * @param messages the list of message maps
     */
    public void setInputMessages(Span span, List<Map<String, String>> messages) {
        if (!config.isHideInputMessages() && messages != null && !messages.isEmpty()) {
            span.setAttribute(SemanticConventions.LLM_INPUT_MESSAGES,
                truncateIfNeeded(gson.toJson(messages)));
        }
    }

    /**
     * Sets output messages on a span as a JSON blob.
     * Each message should be a Map with "role" and "content" keys.
     * @param span the span
     * @param messages the list of message maps
     */
    public void setOutputMessages(Span span, List<Map<String, String>> messages) {
        if (!config.isHideOutputMessages() && messages != null && !messages.isEmpty()) {
            span.setAttribute(SemanticConventions.LLM_OUTPUT_MESSAGES,
                truncateIfNeeded(gson.toJson(messages)));
        }
    }

    /**
     * Creates a message map with role and content.
     * Convenience helper for building message lists.
     * @param role the message role
     * @param content the message content
     * @return a map with role and content entries
     */
    public static Map<String, String> message(String role, String content) {
        Map<String, String> msg = new LinkedHashMap<>();
        msg.put("role", role);
        if (content != null) {
            msg.put("content", content);
        }
        return msg;
    }

    /**
     * Sets error information on a span.
     * @param span the span
     * @param error the error
     */
    public void setError(Span span, Throwable error) {
        span.setStatus(StatusCode.ERROR, error.getMessage());
        span.recordException(error);
        span.setAttribute(SemanticConventions.ERROR_TYPE, error.getClass().getName());
        span.setAttribute(SemanticConventions.ERROR_MESSAGE, error.getMessage());
    }

    /**
     * Executes an operation within a traced span.
     * @param name the span name
     * @param kind the span kind
     * @param operation the operation to execute
     * @param <T> the return type
     * @return the operation result
     */
    public <T> T trace(String name, FISpanKind kind, Supplier<T> operation) {
        Span span = startSpan(name, kind);
        try (Scope scope = span.makeCurrent()) {
            T result = operation.get();
            span.setStatus(StatusCode.OK);
            return result;
        } catch (Exception e) {
            setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Executes an operation within a traced span (no return value).
     * @param name the span name
     * @param kind the span kind
     * @param operation the operation to execute
     */
    public void trace(String name, FISpanKind kind, Runnable operation) {
        Span span = startSpan(name, kind);
        try (Scope scope = span.makeCurrent()) {
            operation.run();
            span.setStatus(StatusCode.OK);
        } catch (Exception e) {
            setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Converts an object to JSON string.
     * @param obj the object to serialize
     * @return the JSON string
     */
    public String toJson(Object obj) {
        if (obj == null) {
            return null;
        }
        if (obj instanceof String) {
            return (String) obj;
        }
        try {
            return gson.toJson(obj);
        } catch (Exception e) {
            // Some SDK classes (e.g., Azure SDK) have conflicting field names in their
            // class hierarchy that cause Gson serialization to fail. Fall back to toString().
            return obj.toString();
        }
    }

    /**
     * Gets the underlying OpenTelemetry tracer.
     * @return the tracer
     */
    public Tracer getTracer() {
        return tracer;
    }

    /**
     * Gets the trace configuration.
     * @return the configuration
     */
    public TraceConfig getConfig() {
        return config;
    }

    /**
     * Truncates a string if it exceeds the maximum length.
     * @param value the value to potentially truncate
     * @return the potentially truncated value
     */
    private String truncateIfNeeded(String value) {
        int maxLength = 32000; // OpenTelemetry attribute value limit
        if (value != null && value.length() > maxLength) {
            return value.substring(0, maxLength - 13) + "...[truncated]";
        }
        return value;
    }
}
