package ai.traceai.openai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.context.Scope;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/** Bounded, best-effort wire metadata. Never changes an SDK payload or throws parsing errors. */
final class OpenAITrace {
    static final ContextKey<Boolean> LEGACY = ContextKey.named("traceai-openai-legacy");
    static final ContextKey<Map<String, String>> FI_ATTRIBUTES = ContextKey.named("traceai-openai-fi-attributes");
    static final int LIMIT = 64 * 1024;
    final FITracer tracer;
    final Span span;
    final Context context;
    private final AtomicBoolean ended = new AtomicBoolean();
    private final StringBuilder text = new StringBuilder();
    private final ResponsesToolTrace tools;
    private boolean failed;
    private boolean complete;

    OpenAITrace(FITracer tracer, String operation) {
        this.tracer = tracer;
        Context parent = Context.current();
        Map<String, String> attributes = parent.get(FI_ATTRIBUTES);
        if (attributes == null) attributes = ai.traceai.ContextAttributes.getAttributesFromContext();
        span = tracer.startSpan("OpenAI " + operation,
            "embeddings".equals(operation) ? FISpanKind.EMBEDDING : FISpanKind.LLM, parent, attributes);
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
        span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, operation);
        context = parent.with(span);
        tools = new ResponsesToolTrace(tracer, span, context, attributes);
    }

    Scope makeCurrent() { return context.makeCurrent(); }

    synchronized void request(byte[] bytes) {
        safely(() -> {
            JsonObject json = JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8)).getAsJsonObject();
            redactCredentials(json);
            attribute(json, "model", SemanticConventions.LLM_MODEL_NAME);
            attribute(json, "model", SemanticConventions.LLM_REQUEST_MODEL);
            if (json.has("messages")) {
                if (!tracer.getConfig().isHideInputs() && !tracer.getConfig().isHideInputMessages()) {
                    tracer.setRawInput(span, json.toString());
                    tracer.setInputMessages(span, messages(json.get("messages")));
                    tracer.setInputValue(span, json.get("messages").toString());
                }
            } else if (!tracer.getConfig().isHideInputMessages()) {
                tracer.setRawInput(span, json.toString());
                if (json.has("input")) tracer.setInputValue(span, json.get("input").toString());
            }
        });
    }

    synchronized boolean response(byte[] bytes) {
        if (bytes == null || ended.get()) return false;
        try {
            json(JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8)).getAsJsonObject(), true);
            return true;
        } catch (RuntimeException ignored) {
            // A partial or invalid capture cannot establish a successful response.
            span.setAttribute("traceai.capture.skipped", true);
            return false;
        }
    }

    synchronized void event(String data) {
        if (ended.get()) return;
        if ("[DONE]".equals(data)) {
            complete = true;
            return;
        }
        safely(() -> {
            JsonObject event = JsonParser.parseString(data).getAsJsonObject();
            String type = string(event, "type");
            if ("response.output_text.delta".equals(type)) append(string(event, "delta"));
            if ("error".equals(type) || "response.failed".equals(type)) {
                failed = true;
                span.setStatus(StatusCode.ERROR);
                span.setAttribute(SemanticConventions.ERROR_TYPE, "openai.stream_error");
            }
            if ("response.incomplete".equals(type)) incomplete();
            boolean terminal = "response.completed".equals(type) || "response.failed".equals(type) || "response.incomplete".equals(type);
            if (terminal) complete = true;
            json(event.has("response") && event.get("response").isJsonObject()
                ? event.getAsJsonObject("response") : event, terminal);
        });
    }

    private void incomplete() {
        failed = true;
        span.setStatus(StatusCode.ERROR);
        span.setAttribute(SemanticConventions.ERROR_TYPE, "openai.response_incomplete");
    }

    private void json(JsonObject json, boolean terminal) {
        attribute(json, "model", SemanticConventions.LLM_RESPONSE_MODEL);
        attribute(json, "id", SemanticConventions.LLM_RESPONSE_ID);
        if ("incomplete".equals(string(json, "status"))) incomplete();
        if ("failed".equals(string(json, "status")) || (json.has("error") && !json.get("error").isJsonNull())) {
            failed = true;
            span.setStatus(StatusCode.ERROR);
            span.setAttribute(SemanticConventions.ERROR_TYPE, "openai.api_error");
        }
        // Failed/incomplete responses can still be billed. Metadata is independent of content
        // capture, and malformed optional telemetry must not mask the response's error status.
        safely(() -> usage(json));
        if (terminal) safely(() -> tools.record(json));
        if (failed) return;
        // These trees were parsed from a telemetry-only byte capture, not the SDK response.
        // Echoed request fields keep their input policy even inside raw output.
        redactCredentials(json);
        if (tracer.getConfig().isHideInputs() || tracer.getConfig().isHideInputMessages()) {
            json.remove("instructions");
        }
        // Error payloads are never model output. Apply tool privacy to parent capture as well.
        output(tools.redact(json));
    }

    /** Authentication in MCP tool definitions is never content, including echoed definitions. */
    private static void redactCredentials(JsonElement value) {
        if (value.isJsonArray()) {
            for (JsonElement child : value.getAsJsonArray()) redactCredentials(child);
        } else if (value.isJsonObject()) {
            JsonObject object = value.getAsJsonObject();
            if ("mcp".equals(string(object, "type"))) {
                object.remove("authorization");
                // Header names are user-defined; removing the whole map also covers custom auth.
                object.remove("headers");
            }
            for (Map.Entry<String, JsonElement> field : object.entrySet()) redactCredentials(field.getValue());
        }
    }

    private void usage(JsonObject json) {
        if (json.has("usage") && json.get("usage").isJsonObject()) {
            JsonObject usage = json.getAsJsonObject("usage");
            count(usage, "prompt_tokens", SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
            count(usage, "input_tokens", SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
            count(usage, "completion_tokens", SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
            count(usage, "output_tokens", SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
            count(usage, "total_tokens", SemanticConventions.LLM_TOKEN_COUNT_TOTAL);
            // These are subsets of aggregate usage, not extra tokens to add to totals.
            detail(usage, "prompt_tokens_details", "cached_tokens", SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ);
            detail(usage, "input_tokens_details", "cached_tokens", SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ);
            detail(usage, "completion_tokens_details", "reasoning_tokens", SemanticConventions.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING);
            detail(usage, "output_tokens_details", "reasoning_tokens", SemanticConventions.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING);
        }
    }

    private void output(JsonObject json) {
        if (json.has("choices") && json.get("choices").isJsonArray()) {
            List<Map<String, String>> output = new ArrayList<>();
            for (JsonElement item : json.getAsJsonArray("choices")) {
                JsonObject choice = item.getAsJsonObject();
                attribute(choice, "finish_reason", SemanticConventions.LLM_RESPONSE_FINISH_REASON);
                if (choice.has("message")) output.add(message(choice.getAsJsonObject("message")));
                // Primary text is bounded across all deltas; raw chunks are never retained.
                if (choice.has("delta") && (!choice.has("index") || choice.get("index").getAsInt() == 0)) {
                    append(string(choice.getAsJsonObject("delta"), "content"));
                }
            }
            tracer.setOutputMessages(span, output);
            if (!output.isEmpty() && !tracer.getConfig().isHideOutputMessages()) {
                tracer.setOutputValue(span, output.get(0).get("content"));
            }
        }
        if (json.has("output") && !tracer.getConfig().isHideOutputMessages()) {
            tracer.setOutputValue(span, json.get("output").toString());
        }
        if (!json.has("type") && !"chat.completion.chunk".equals(string(json, "object"))
                && !tracer.getConfig().isHideOutputMessages()) {
            tracer.setRawOutput(span, json.toString());
        }
    }

    private void append(String value) {
        if (value != null && !tracer.getConfig().isHideOutputs() && !tracer.getConfig().isHideOutputMessages()) {
            int available = 32000 - text.length();
            text.append(value, 0, Math.min(value.length(), available));
            if (value.length() > available) span.setAttribute("traceai.capture.truncated", true);
        }
    }

    synchronized void status(int status) {
        span.setAttribute("http.response.status_code", status);
        if (status >= 400) {
            failed = true;
            span.setStatus(StatusCode.ERROR);
            span.setAttribute(SemanticConventions.ERROR_TYPE, "http." + status);
        }
    }

    synchronized void error(Throwable error) {
        if (ended.get()) return;
        failed = true;
        safely(() -> tracer.setError(span, error));
    }

    synchronized void end(boolean exhausted) {
        if (!ended.compareAndSet(false, true)) return;
        try {
            if (text.length() > 0) {
                tracer.setOutputValue(span, text.toString());
                tracer.setOutputMessages(span, List.of(FITracer.message("assistant", text.toString())));
            }
            if (!failed && (exhausted || complete)) span.setStatus(StatusCode.OK);
            if (!exhausted && !complete) span.setAttribute("traceai.response.closed_early", true);
        } finally {
            span.end();
        }
    }

    private void attribute(JsonObject json, String field, String key) {
        String value = string(json, field);
        if (value != null) span.setAttribute(key, value);
    }

    private void count(JsonObject json, String field, String key) {
        if (json.has(field) && json.get(field).isJsonPrimitive()) span.setAttribute(key, json.get(field).getAsLong());
    }

    private void detail(JsonObject usage, String group, String field, String key) {
        JsonElement details = usage.get(group);
        if (details == null || !details.isJsonObject()) return;
        JsonElement value = details.getAsJsonObject().get(field);
        if (value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isNumber()) {
            count(details.getAsJsonObject(), field, key);
        }
    }

    private static String string(JsonObject json, String key) {
        JsonElement value = json.get(key);
        return value != null && value.isJsonPrimitive() ? value.getAsString() : null;
    }

    private static List<Map<String, String>> messages(JsonElement value) {
        List<Map<String, String>> result = new ArrayList<>();
        if (value.isJsonArray()) for (JsonElement item : value.getAsJsonArray()) result.add(message(item.getAsJsonObject()));
        return result;
    }

    private static Map<String, String> message(JsonObject value) {
        JsonElement content = value.get("content");
        return FITracer.message(string(value, "role"), content == null || content.isJsonNull() ? null
            : content.isJsonPrimitive() ? content.getAsString() : content.toString());
    }

    // Kotlin SDK interfaces can throw checked failures through Java methods without throws clauses.
    @SuppressWarnings("unchecked")
    static <E extends Throwable> RuntimeException propagate(Throwable failure) throws E {
        throw (E) failure;
    }

    private void safely(Runnable action) {
        try { action.run(); }
        catch (RuntimeException ignored) {
            // Fail open without logging payload-bearing exceptions outside TraceConfig's privacy policy.
            span.setAttribute("traceai.capture.skipped", true);
        }
    }

    /** Reject oversized JSON rather than parsing a truncated, misleading document. */
    static final class Capture extends ByteArrayOutputStream {
        private boolean overflow;
        @Override public synchronized void write(int b) {
            if (count < LIMIT) super.write(b); else overflow = true;
        }
        @Override public synchronized void write(byte[] b, int off, int len) {
            int remaining = LIMIT - count;
            super.write(b, off, Math.min(remaining, len));
            if (len > remaining) overflow = true;
        }
        byte[] completeBytes() { return overflow ? null : toByteArray(); }
        @Override public synchronized void reset() { super.reset(); overflow = false; }
    }
}
