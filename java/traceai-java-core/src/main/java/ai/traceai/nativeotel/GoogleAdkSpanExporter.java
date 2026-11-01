package ai.traceai.nativeotel;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributesBuilder;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.DelegatingSpanData;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.data.StatusData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Set;

/** Destination-local projection of ADK 1.12/1.13 spans; never creates spans or mutates originals. */
final class GoogleAdkSpanExporter implements SpanExporter {
    private static final String PREFIX = "gcp.vertex.agent.";
    private static final Set<String> PRIVATE_KEYS = Set.of(
            PREFIX + "llm_request", PREFIX + "llm_response", PREFIX + "tool_call_args",
            PREFIX + "tool_response", PREFIX + "data", "input.value", "input.mime_type",
            "output.value", "output.mime_type", "fi.raw_input", "fi.raw_output",
            "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
            "gen_ai.tool.call.arguments", "gen_ai.tool.call.result", "gen_ai.tool.definitions",
            "error.message", "exception.message", "exception.stacktrace");
    private final SpanExporter delegate;
    private final boolean captureContent;

    GoogleAdkSpanExporter(SpanExporter delegate, boolean captureContent) {
        this.delegate = delegate;
        this.captureContent = captureContent;
    }

    @Override
    public CompletableResultCode export(Collection<SpanData> spans) {
        List<SpanData> projected = new ArrayList<>(spans.size());
        for (SpanData span : spans) {
            projected.add(GoogleAdkTelemetry.ADK_SCOPE.equals(span.getInstrumentationScopeInfo().getName())
                    ? new AdkSpan(span, attributes(span), captureContent) : span);
        }
        return delegate.export(projected);
    }

    private Attributes attributes(SpanData span) {
        Attributes original = span.getAttributes();
        AttributesBuilder result = Attributes.builder();
        original.forEach((key, value) -> {
            if (captureContent || !PRIVATE_KEYS.contains(key.getKey())) {
                put(result, key, value);
            }
        });
        String kind = kind(span.getName(), string(original, "gen_ai.operation.name"));
        putIfMissing(result, original, "fi.span.kind", kind);
        putIfMissing(result, original, "session.id", string(original, PREFIX + "session_id"));
        putIfMissing(result, original, "gen_ai.tool.call.id", string(original, "gen_ai.tool_call.id"));
        if (captureContent) {
            if ("TOOL".equals(kind)) {
                mapJson(result, original, PREFIX + "tool_call_args", "input", "gen_ai.tool.call.arguments");
                mapJson(result, original, PREFIX + "tool_response", "output", "gen_ai.tool.call.result");
            } else if ("LLM".equals(kind)) {
                JsonObject request = object(mapJson(result, original, PREFIX + "llm_request", "input", null));
                JsonObject response = object(mapJson(result, original, PREFIX + "llm_response", "output", null));
                JsonArray input = new JsonArray();
                JsonObject config = object(request == null ? null : request.get("config"));
                appendMessage(input, field(config, "systemInstruction", "system_instruction"), "system");
                JsonElement contents = request == null ? null : request.get("contents");
                if (contents != null && contents.isJsonArray()) {
                    contents.getAsJsonArray().forEach(content -> appendMessage(input, content, "user"));
                }
                JsonArray output = new JsonArray();
                appendMessage(output, response == null ? null : response.get("content"), "assistant");
                if (!input.isEmpty()) {
                    putIfMissing(result, original, "gen_ai.input.messages", input.toString());
                }
                if (!output.isEmpty()) {
                    putIfMissing(result, original, "gen_ai.output.messages", output.toString());
                }
            }
        }
        return result.build();
    }

    private static JsonElement mapJson(AttributesBuilder result, Attributes original, String source,
                                       String direction, String semanticKey) {
        String raw = string(original, source);
        JsonElement parsed = parse(raw);
        if (parsed == null || !(parsed.isJsonObject() || parsed.isJsonArray())) {
            return null; // ADK placeholders, truncated attributes and future formats remain raw only.
        }
        if (string(original, direction + ".value") == null) {
            result.put(direction + ".value", raw).put(direction + ".mime_type", "application/json");
        }
        if (semanticKey != null) {
            putIfMissing(result, original, semanticKey, raw);
        }
        return parsed;
    }

    private static void appendMessage(JsonArray messages, JsonElement element, String fallbackRole) {
        JsonObject content = object(element);
        if (content == null || !content.has("parts") || !content.get("parts").isJsonArray()) {
            return;
        }
        JsonArray parts = new JsonArray();
        for (JsonElement item : content.getAsJsonArray("parts")) {
            JsonObject part = object(item);
            if (part == null) {
                continue;
            }
            if (part.has("text") && part.get("text").isJsonPrimitive()) {
                JsonObject text = new JsonObject();
                text.addProperty("type", "text");
                text.add("content", part.get("text"));
                parts.add(text);
            }
            appendToolPart(parts, field(part, "functionCall", "function_call"), "tool_call", "args", "arguments");
            appendToolPart(parts, field(part, "functionResponse", "function_response"), "tool_call_response", "response", "response");
        }
        // Unsupported media parts remain available in the original structured input/output.
        if (!parts.isEmpty()) {
            JsonObject message = new JsonObject();
            String role = content.has("role") && content.get("role").isJsonPrimitive()
                    ? content.get("role").getAsString() : fallbackRole;
            message.addProperty("role", "model".equals(role) ? "assistant" : role);
            message.add("parts", parts);
            messages.add(message);
        }
    }

    private static void appendToolPart(JsonArray parts, JsonElement element, String type,
                                       String sourcePayload, String targetPayload) {
        JsonObject function = object(element);
        if (function == null) {
            return;
        }
        JsonObject part = new JsonObject();
        part.addProperty("type", type);
        for (String key : new String[] {"name", "id"}) {
            if (function.has(key)) {
                part.add(key, function.get(key));
            }
        }
        if (function.has(sourcePayload)) {
            part.add(targetPayload, function.get(sourcePayload));
        }
        parts.add(part);
    }

    private static JsonElement field(JsonObject object, String camel, String snake) {
        return object == null ? null : object.has(camel) ? object.get(camel) : object.get(snake);
    }

    private static JsonObject object(JsonElement element) {
        return element != null && element.isJsonObject() ? element.getAsJsonObject() : null;
    }

    private static JsonElement parse(String raw) {
        if (raw == null || raw.length() > 1_048_576) {
            return null; // Bound extra mapping work; leave the native attribute untouched.
        }
        try {
            return JsonParser.parseString(raw);
        } catch (RuntimeException e) {
            return null; // Telemetry shape changes must not break export; never log content.
        }
    }

    private static String kind(String name, String operation) {
        if ("invoke_agent".equals(operation) || name.startsWith("invoke_agent ")) return "AGENT";
        if ("call_llm".equals(operation) || "call_llm".equals(name)) return "LLM";
        if ("execute_tool".equals(operation) || name.startsWith("execute_tool ")) return "TOOL";
        if ("invocation".equals(name) || name.startsWith("invoke_workflow")) return "CHAIN";
        return null;
    }

    private static String string(Attributes attributes, String name) {
        return attributes.get(AttributeKey.stringKey(name));
    }

    private static void putIfMissing(AttributesBuilder result, Attributes original, String key, String value) {
        if (value != null && string(original, key) == null) {
            result.put(key, value);
        }
    }

    @SuppressWarnings("unchecked")
    private static <T> void put(AttributesBuilder result, AttributeKey<T> key, Object value) {
        result.put(key, (T) value);
    }

    @Override public CompletableResultCode flush() { return delegate.flush(); }
    @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }

    private static final class AdkSpan extends DelegatingSpanData {
        private final Attributes attributes;
        private final boolean captureContent;

        AdkSpan(SpanData original, Attributes attributes, boolean captureContent) {
            super(original);
            this.attributes = attributes;
            this.captureContent = captureContent;
        }

        @Override public Attributes getAttributes() { return attributes; }
        @Override public int getTotalAttributeCount() {
            // Preserve the SDK's dropped-attribute count while accounting for the projection.
            return super.getTotalAttributeCount() - super.getAttributes().size() + attributes.size();
        }
        @Override public StatusData getStatus() {
            return captureContent ? super.getStatus() : StatusData.create(super.getStatus().getStatusCode(), "");
        }
        @Override public List<EventData> getEvents() { return captureContent ? super.getEvents() : List.of(); }
        @Override public int getTotalRecordedEvents() { return captureContent ? super.getTotalRecordedEvents() : 0; }
    }
}
