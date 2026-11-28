package ai.traceai.openai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import java.time.Instant;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

/** Metadata-only markers for server-executed tools, not measurements of their execution time. */
final class ResponsesToolTrace {
    private static final int MAX_MARKERS = 64;
    private static final int MAX_ID_LENGTH = 256;
    private static final Set<String> TYPES = Set.of("web_search_call", "file_search_call", "code_interpreter_call", "mcp_call");
    private final FITracer tracer;
    private final Span parent;
    private final Context context;
    private final Map<String, String> attributes;
    private final Set<String> recorded = new HashSet<>();

    ResponsesToolTrace(FITracer tracer, Span parent, Context context, Map<String, String> attributes) {
        this.tracer = tracer;
        this.parent = parent;
        this.context = context;
        this.attributes = Map.copyOf(attributes);
    }

    void record(JsonObject response) {
        JsonElement output = response.get("output");
        if (output == null || !output.isJsonArray()) return;
        for (JsonElement element : output.getAsJsonArray()) {
            if (!element.isJsonObject()) continue;
            JsonObject item = element.getAsJsonObject();
            String type = string(item, "type");
            if (type == null || !TYPES.contains(type)) continue;
            String id = string(item, "id");
            if (id == null || id.isEmpty() || id.length() > MAX_ID_LENGTH) continue;
            String status = string(item, "status");
            boolean error = item.has("error") && !item.get("error").isJsonNull();
            if (error) status = "failed";
            // MCP output items may omit status. A returned result is terminal, an empty item isn't.
            if (status == null && type.equals("mcp_call") && item.has("output") && !item.get("output").isJsonNull()) {
                status = "completed";
            }
            if (!"completed".equals(status) && !"failed".equals(status)
                    && !"incomplete".equals(status) && !"cancelled".equals(status)) continue;
            String key = type + ":" + id;
            if (recorded.contains(key)) continue;
            if (recorded.size() >= MAX_MARKERS) {
                parent.setAttribute("traceai.tool.markers.truncated", true);
                continue;
            }
            recorded.add(key);
            String name = type.equals("mcp_call") ? string(item, "name") : null;
            marker(type, id, status, name);
        }
    }

    private void marker(String type, String id, String status, String toolName) {
        String name = type.substring(0, type.length() - "_call".length());
        if (toolName == null || toolName.isEmpty() || toolName.length() > MAX_ID_LENGTH) toolName = name;
        // The API supplies no execution timestamps. Use the same instant for start and end,
        // rather than inventing a duration or attributing the whole LLM latency to every tool.
        Instant observed = Instant.now();
        Span tool = tracer.getTracer().spanBuilder("OpenAI " + name).setParent(context)
            .setSpanKind(SpanKind.INTERNAL).setStartTimestamp(observed).startSpan();
        try {
            attributes.forEach(tool::setAttribute);
            tool.setAttribute(SemanticConventions.FI_SPAN_KIND, FISpanKind.TOOL.getValue());
            tool.setAttribute(SemanticConventions.GEN_AI_SPAN_KIND, "tool");
            tool.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            tool.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "execute_tool");
            tool.setAttribute(SemanticConventions.GEN_AI_TOOL_NAME, toolName);
            tool.setAttribute(SemanticConventions.GEN_AI_TOOL_CALL_ID, id);
            tool.setAttribute("gen_ai.tool.type", type);
            tool.setAttribute("openai.tool.status", status);
            tool.setAttribute("traceai.tool.marker", true);
            if (status.equals("completed")) tool.setStatus(StatusCode.OK);
            else {
                tool.setStatus(StatusCode.ERROR);
                tool.setAttribute(SemanticConventions.ERROR_TYPE, "openai.tool_error");
            }
            // Queries, code, arguments, results and error messages never enter marker attributes.
        } finally {
            tool.end(observed);
        }
    }

    /** Tool inputs and errors also occur inside the parent output; do not bypass privacy there. */
    JsonObject redact(JsonObject response) {
        boolean hideInput = tracer.getConfig().isHideInputs() || tracer.getConfig().isHideInputMessages();
        boolean hideErrors = tracer.getConfig().isHideErrorDetails();
        JsonElement output = response.get("output");
        if ((!hideInput && !hideErrors) || output == null || !output.isJsonArray()) return response;
        JsonObject captured = response.deepCopy();
        for (JsonElement element : captured.getAsJsonArray("output")) {
            if (!element.isJsonObject()) continue;
            JsonObject item = element.getAsJsonObject();
            String type = string(item, "type");
            if (type == null || !TYPES.contains(type)) continue;
            if (hideInput) {
                item.remove("action");
                item.remove("queries");
                item.remove("code");
                item.remove("arguments");
            }
            if (hideErrors) item.remove("error");
        }
        return captured;
    }

    private static String string(JsonObject object, String field) {
        JsonElement value = object.get(field);
        return value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isString() ? value.getAsString() : null;
    }
}
