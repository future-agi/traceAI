package ai.traceai.googleadk;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonElement;
import com.google.gson.JsonParser;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Maps what Google ADK for Java reports about an agent run to the shared traceAI attributes.
 *
 * <p>The inputs are the JSON shapes ADK itself serializes (an LLM request with {@code model}, {@code config} and
 * {@code contents}, an LLM response with {@code content} and {@code usageMetadata}, tool arguments and results), parsed
 * into maps, so the mapping does not depend on ADK classes and serves both the library wrappers
 * ({@link TracedLlm}, {@link TracedTool}) and the zero-code agent.
 *
 * <p>Every method returns attributes only; the caller applies its privacy rules (hide switches, known-secret
 * replacement) when it writes them.
 */
public final class AdkAttributes {
    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();

    private AdkAttributes() {}

    /** Parses JSON into maps, lists, strings, numbers and booleans; null when the text is not JSON. */
    public static Object parseJson(String json) {
        if (json == null) return null;
        try {
            return toPlain(JsonParser.parseString(json));
        } catch (RuntimeException e) {
            return null;
        }
    }

    /** The text a value that cannot be serialized is replaced by; it names no content. */
    static final String UNSERIALIZABLE = "<unserializable>";
    private static final int MAX_DEPTH = 32;
    private static final int MAX_NODES = 20_000;

    /**
     * JSON text for a value. Never throws: maps, collections, arrays, strings, numbers and booleans are written; a cycle, a
     * value nested too deeply or too large, and any other type (java.time values, application objects) are each replaced by
     * a fixed placeholder, so tracing a call can never fail it.
     */
    public static String toJson(Object value) {
        try {
            return GSON.toJson(plain(value, 0, new int[] {MAX_NODES}, new java.util.IdentityHashMap<>()));
        } catch (Throwable e) {
            return "\"" + UNSERIALIZABLE + "\"";
        }
    }

    private static Object plain(Object value, int depth, int[] budget, java.util.IdentityHashMap<Object, Boolean> path) {
        if (--budget[0] < 0 || depth > MAX_DEPTH) return "<truncated>";
        if (value == null || value instanceof String || value instanceof Boolean) return value;
        if (value instanceof Number n) {
            double d = n.doubleValue();
            return Double.isNaN(d) || Double.isInfinite(d) ? (Object) String.valueOf(d) : value;
        }
        // Kept as their values, as Gson wrote them.
        if (value instanceof Enum<?> e) return e.name();
        if (value instanceof Character || value instanceof java.util.UUID) return String.valueOf(value);
        boolean container = value instanceof Map || value instanceof Iterable || value.getClass().isArray();
        if (!container) return "<unserializable: " + value.getClass().getName() + ">";
        if (path.put(value, Boolean.TRUE) != null) return "<cycle>";
        try {
            if (value instanceof Map<?, ?> map) {
                Map<String, Object> out = new LinkedHashMap<>();
                for (Map.Entry<?, ?> e : map.entrySet()) {
                    // Once the budget is spent the rest is never walked: one placeholder stands for it.
                    if (budget[0] <= 0) { out.put("<truncated>", "<truncated>"); break; }
                    out.put(String.valueOf(e.getKey()), plain(e.getValue(), depth + 1, budget, path));
                }
                return out;
            }
            List<Object> out = new ArrayList<>();
            if (value instanceof Iterable<?> items) {
                for (Object item : items) {
                    if (budget[0] <= 0) { out.add("<truncated>"); break; }
                    out.add(plain(item, depth + 1, budget, path));
                }
            } else {
                for (int i = 0, n = java.lang.reflect.Array.getLength(value); i < n; i++) {
                    if (budget[0] <= 0) { out.add("<truncated>"); break; }
                    out.add(plain(java.lang.reflect.Array.get(value, i), depth + 1, budget, path));
                }
            }
            return out;
        } finally {
            path.remove(value);
        }
    }

    private static Object toPlain(JsonElement element) {
        if (element == null || element.isJsonNull()) return null;
        if (element.isJsonObject()) {
            Map<String, Object> map = new LinkedHashMap<>();
            element.getAsJsonObject().entrySet().forEach(e -> map.put(e.getKey(), toPlain(e.getValue())));
            return map;
        }
        if (element.isJsonArray()) {
            List<Object> list = new ArrayList<>();
            element.getAsJsonArray().forEach(e -> list.add(toPlain(e)));
            return list;
        }
        if (element.getAsJsonPrimitive().isBoolean()) return element.getAsBoolean();
        if (element.getAsJsonPrimitive().isNumber()) {
            double value = element.getAsDouble();
            return value == Math.rint(value) && Math.abs(value) < 9e15 ? (Object) (long) value : (Object) value;
        }
        return element.getAsString();
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Object value) {
        return value instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
    }

    @SuppressWarnings("unchecked")
    private static List<Object> list(Object value) {
        return value instanceof List<?> l ? (List<Object>) l : List.of();
    }

    private static String string(Object value) {
        return value == null ? null : String.valueOf(value);
    }

    private static Long number(Object value) {
        return value instanceof Number n ? Long.valueOf(n.longValue()) : null;
    }

    private static void put(Map<String, Object> attrs, String key, Object value) {
        if (value == null) return;
        if (value instanceof String s && s.isEmpty()) return;
        attrs.put(key, value);
    }

    /** A provider name for a model name, or null when it is not recognizable. */
    public static String provider(String model) {
        if (model == null) return null;
        String name = model.toLowerCase(Locale.ROOT);
        int slash = name.lastIndexOf('/');
        if (slash >= 0) name = name.substring(slash + 1);
        if (name.startsWith("gemini") || name.startsWith("gemma")) return "gcp.gemini";
        if (name.startsWith("claude")) return "anthropic";
        if (name.startsWith("gpt") || name.startsWith("o1") || name.startsWith("o3") || name.startsWith("o4")) return "openai";
        if (name.startsWith("llama") || name.startsWith("meta-llama")) return "meta";
        if (name.startsWith("mistral")) return "mistral_ai";
        return null;
    }

    // ---- LLM calls -------------------------------------------------------------------------------------------

    /**
     * Attributes of one model call.
     *
     * @param request  the request as ADK serializes it ({@code model}, {@code config}, {@code contents}); may be null
     * @param response the response as ADK serializes it ({@code content}, {@code usageMetadata}, ...); may be null
     */
    public static Map<String, Object> llm(Map<String, Object> request, Map<String, Object> response) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", "LLM");
        attrs.put("gen_ai.operation.name", "chat");
        request = request == null ? Map.of() : request;
        response = response == null ? Map.of() : response;
        String model = string(request.get("model"));
        put(attrs, "gen_ai.request.model", model);
        put(attrs, "gen_ai.provider.name", provider(model));

        Map<String, Object> config = map(request.get("config"));
        put(attrs, "gen_ai.request.temperature", config.get("temperature"));
        put(attrs, "gen_ai.request.top_p", config.get("topP"));
        put(attrs, "gen_ai.request.max_tokens", number(config.get("maxOutputTokens")));
        Map<String, Object> parameters = new LinkedHashMap<>(config);
        parameters.remove("systemInstruction");
        parameters.remove("tools");
        if (!parameters.isEmpty()) put(attrs, "gen_ai.request.parameters", toJson(parameters));

        List<Map<String, Object>> input = new ArrayList<>();
        Map<String, Object> system = map(config.get("systemInstruction"));
        String systemText = text(system);
        if (systemText != null) input.add(message("system", systemText, List.of(), null));
        for (Object content : list(request.get("contents"))) input.addAll(messages(map(content)));
        writeMessages(attrs, "gen_ai.input.messages", input);

        int tool = 0;
        for (Object entry : list(config.get("tools"))) {
            for (Object declaration : list(map(entry).get("functionDeclarations"))) {
                attrs.put("gen_ai.tool.definitions." + tool++ + ".tool.json_schema", toJson(declaration));
            }
        }

        List<Map<String, Object>> output = messages(map(response.get("content")));
        writeMessages(attrs, "gen_ai.output.messages", output);

        Map<String, Object> usage = map(response.get("usageMetadata"));
        Long prompt = number(usage.get("promptTokenCount")), toolUse = number(usage.get("toolUsePromptTokenCount"));
        Long candidates = number(usage.get("candidatesTokenCount")), thoughts = number(usage.get("thoughtsTokenCount"));
        Long input_tokens = prompt != null || toolUse != null ? Long.valueOf(nz(prompt) + nz(toolUse)) : null;
        Long output_tokens = candidates != null || thoughts != null ? Long.valueOf(nz(candidates) + nz(thoughts)) : null;
        Long total = number(usage.get("totalTokenCount"));
        if (total == null && input_tokens != null && output_tokens != null) total = input_tokens + output_tokens;
        put(attrs, "gen_ai.usage.input_tokens", input_tokens);
        put(attrs, "gen_ai.usage.output_tokens", output_tokens);
        put(attrs, "gen_ai.usage.total_tokens", total);
        put(attrs, "gen_ai.usage.output_tokens.reasoning", thoughts);
        put(attrs, "gen_ai.usage.cache_read_tokens", number(usage.get("cachedContentTokenCount")));
        String finish = string(response.get("finishReason"));
        if (finish != null && !finish.isEmpty()) attrs.put("gen_ai.response.finish_reasons", List.of(finish.toLowerCase(Locale.ROOT)));

        String prompted = lastUserText(request);
        if (prompted != null) {
            attrs.put("input.value", prompted);
            attrs.put("input.mime_type", "text/plain");
        }
        if (!output.isEmpty()) {
            Map<String, Object> first = output.get(0);
            String content = string(first.get("content"));
            if (content != null && !content.isEmpty()) {
                attrs.put("output.value", content);
                attrs.put("output.mime_type", "text/plain");
            } else if (first.get("tool_calls") instanceof List<?> calls && !calls.isEmpty()) {
                attrs.put("output.value", toJson(calls));
                attrs.put("output.mime_type", "application/json");
            }
        }
        return attrs;
    }

    /** The error message of a failed model call, from the response's error fields, or null. */
    public static String llmError(Map<String, Object> response) {
        if (response == null) return null;
        Object message = response.get("errorMessage"), code = response.get("errorCode");
        if (message == null && code == null) return null;
        return message != null ? String.valueOf(message) : String.valueOf(code);
    }

    private static long nz(Long value) {
        return value == null ? 0 : value;
    }

    private static String lastUserText(Map<String, Object> request) {
        String found = null;
        for (Object content : list(request.get("contents"))) {
            Map<String, Object> c = map(content);
            if (!"user".equals(string(c.get("role")))) continue;
            String text = text(c);
            if (text != null) found = text;
        }
        return found;
    }

    /** The text parts of a content joined with newlines, or null when it has none. */
    public static String text(Map<String, Object> content) {
        StringBuilder text = new StringBuilder();
        for (Object part : list(content == null ? null : content.get("parts"))) {
            Object t = map(part).get("text");
            if (t instanceof String s && !s.isEmpty() && !Boolean.TRUE.equals(map(part).get("thought"))) {
                if (text.length() > 0) text.append('\n');
                text.append(s);
            }
        }
        return text.length() == 0 ? null : text.toString();
    }

    private static Map<String, Object> message(String role, String content, List<Map<String, Object>> calls, String toolCallId) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("role", role);
        if (content != null) m.put("content", content);
        if (toolCallId != null) m.put("tool_call_id", toolCallId);
        if (!calls.isEmpty()) m.put("tool_calls", calls);
        return m;
    }

    /** One ADK content as messages: text and calls of a model turn are one assistant message, each tool result its own message. */
    private static List<Map<String, Object>> messages(Map<String, Object> content) {
        List<Map<String, Object>> result = new ArrayList<>();
        if (content.isEmpty()) return result;
        String role = "model".equals(string(content.get("role"))) ? "assistant" : "user";
        List<Map<String, Object>> calls = new ArrayList<>();
        for (Object part : list(content.get("parts"))) {
            Map<String, Object> p = map(part);
            Map<String, Object> call = map(p.get("functionCall"));
            if (!call.isEmpty()) {
                Map<String, Object> c = new LinkedHashMap<>();
                put(c, "id", string(call.get("id")));
                put(c, "name", string(call.get("name")));
                c.put("arguments", toJson(call.get("args") == null ? Map.of() : call.get("args")));
                calls.add(c);
            }
            Map<String, Object> response = map(p.get("functionResponse"));
            if (!response.isEmpty()) {
                Map<String, Object> m = message("tool", toJson(response.get("response") == null ? Map.of() : response.get("response")), List.of(), string(response.get("id")));
                put(m, "name", string(response.get("name")));
                result.add(m);
            }
        }
        String text = text(content);
        if (text != null || !calls.isEmpty()) result.add(0, message(role, text, calls, null));
        return result;
    }

    private static void writeMessages(Map<String, Object> attrs, String prefix, List<Map<String, Object>> messages) {
        for (int i = 0; i < messages.size(); i++) {
            Map<String, Object> m = messages.get(i);
            String base = prefix + "." + i + ".message";
            put(attrs, base + ".role", m.get("role"));
            put(attrs, base + ".content", m.get("content"));
            put(attrs, base + ".name", m.get("name"));
            put(attrs, base + ".tool_call_id", m.get("tool_call_id"));
            List<Object> calls = list(m.get("tool_calls"));
            for (int k = 0; k < calls.size(); k++) {
                Map<String, Object> call = map(calls.get(k));
                String b = base + ".tool_calls." + k + ".tool_call";
                put(attrs, b + ".id", call.get("id"));
                put(attrs, b + ".function.name", call.get("name"));
                put(attrs, b + ".function.arguments", call.get("arguments"));
            }
        }
    }

    // ---- Tools, agents and invocations -----------------------------------------------------------------------

    /** Attributes of one tool call; {@code arguments} and {@code result} are the maps ADK passes and returns. */
    public static Map<String, Object> tool(String name, String description, String type, Object arguments, Object result, String callId) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", "TOOL");
        attrs.put("gen_ai.operation.name", "execute_tool");
        put(attrs, "gen_ai.tool.name", name);
        put(attrs, "gen_ai.tool.description", description);
        put(attrs, "gen_ai.tool.type", type);
        put(attrs, "gen_ai.tool.call.id", callId);
        if (arguments != null) {
            String json = arguments instanceof String s ? s : toJson(arguments);
            attrs.put("gen_ai.tool.call.arguments", json);
            attrs.put("input.value", json);
            attrs.put("input.mime_type", "application/json");
        }
        if (result != null) {
            String json = result instanceof String s ? s : toJson(result);
            attrs.put("gen_ai.tool.call.result", json);
            attrs.put("output.value", json);
            attrs.put("output.mime_type", "application/json");
        }
        return attrs;
    }

    /** Attributes of one agent run. */
    public static Map<String, Object> agent(String name, String description) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", "AGENT");
        attrs.put("gen_ai.operation.name", "invoke_agent");
        put(attrs, "gen_ai.agent.name", name);
        put(attrs, "gen_ai.agent.description", description);
        put(attrs, "agent.name", name);
        return attrs;
    }

    /** Attributes of the span that covers a whole invocation of a runner. */
    public static Map<String, Object> invocation() {
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", "CHAIN");
        attrs.put("gen_ai.operation.name", "invocation");
        return attrs;
    }

    /** Session, user and conversation identity. */
    public static Map<String, Object> identity(String sessionId, String userId) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        put(attrs, "session.id", sessionId);
        put(attrs, "gen_ai.conversation.id", sessionId);
        put(attrs, "user.id", userId);
        return attrs;
    }
}
