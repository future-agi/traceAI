package ai.traceai.auto;

import java.util.*;
import static ai.traceai.auto.WireCore.*;

/** Output recognition only: no payload retention, tool execution, or audio playback inference. */
final class OutputMilestone {
    static boolean nonempty(Object value) { return value instanceof String s && !s.isEmpty(); }
    static boolean arguments(Object value) { return nonempty(value) || value instanceof Map<?, ?> m && !m.isEmpty(); }
    static boolean function(Object value) { return nonempty(get(value, "name")) || arguments(get(value, "arguments")); }
    private static List<?> objects(Object value) { return value instanceof List<?> list ? list : value instanceof Map ? List.of(value) : List.of(); }
    private static boolean content(Object value) {
        if (nonempty(value)) return true;
        for (Object part : objects(value)) {
            Object kind = get(part, "type");
            if (kind != null && !kind.equals("text") && !kind.equals("thinking")) continue;
            if (nonempty(get(part, "text")) || nonempty(get(part, "thinking"))) return true;
            for (Object thought : list(get(part, "thinking")))
                if ("text".equals(get(thought, "type")) && nonempty(get(thought, "text"))) return true;
        }
        return false;
    }
    static boolean qualifies(String format, Object event) {
        switch (format) {
            case "openai.chat", "openai.completions":
                for (Object choice : list(get(event, "choices"))) {
                    if (format.equals("openai.completions") && nonempty(get(choice, "text"))) return true;
                    Object delta = get(choice, "delta");
                    if (content(get(delta, "content")) || nonempty(get(delta, "refusal")) || nonempty(get(delta, "reasoning_content")) || nonempty(get(delta, "reasoning"))) return true;
                    Object audio = get(delta, "audio"); if (nonempty(get(audio, "transcript")) || nonempty(get(audio, "data"))) return true;
                    if (function(get(delta, "function_call"))) return true;
                    for (Object call : list(get(delta, "tool_calls"))) if (function(get(call, "function"))) return true;
                }
                return false;
            case "openai.responses": {
                String type = string(get(event, "type"));
                if (type != null && Set.of("response.output_text.delta", "response.refusal.delta", "response.audio.transcript.delta", "response.audio.delta",
                    "response.reasoning_text.delta", "response.reasoning_summary_text.delta", "response.function_call_arguments.delta", "response.mcp_call_arguments.delta",
                    "response.code_interpreter_call_code.delta", "response.custom_tool_call_input.delta").contains(type)) return nonempty(get(event, "delta"));
                if (!"response.output_item.added".equals(type)) return false;
                Object item = get(event, "item"); String kind = string(get(item, "type"));
                if ("code_interpreter_call".equals(kind)) return nonempty(get(item, "code"));
                return kind != null && Set.of("function_call", "tool_call", "custom_tool_call", "mcp_call").contains(kind)
                    && (function(item) || function(get(item, "function")) || "custom_tool_call".equals(kind) && nonempty(get(item, "input")));
            }
            case "anthropic.messages", "bedrock.invoke": {
                String type = string(get(event, "type")); Object delta = get(event, "delta"), block = get(event, "content_block");
                if ("content_block_delta".equals(type)) return nonempty(get(delta, "text")) || nonempty(get(delta, "thinking")) || nonempty(get(delta, "partial_json"));
                if (!"content_block_start".equals(type)) return false;
                String kind = string(get(block, "type"));
                return nonempty(get(block, "text")) || nonempty(get(block, "thinking"))
                    || kind != null && Set.of("tool_use", "server_tool_use", "mcp_tool_use").contains(kind) && nonempty(get(block, "name"));
            }
            case "gemini.generate_content":
                for (Object candidate : list(get(event, "candidates"))) for (Object part : list(get(get(candidate, "content"), "parts"))) {
                    if (nonempty(get(part, "text"))) return true;
                    Object function = get(part, "functionCall");
                    if (nonempty(get(function, "name")) || arguments(get(function, "args")) || nonempty(get(get(part, "executableCode"), "code"))) return true;
                    Object inline = map(part).containsKey("inlineData") ? get(part, "inlineData") : get(part, "inline_data");
                    Object mime = map(inline).containsKey("mimeType") ? get(inline, "mimeType") : get(inline, "mime_type");
                    if (mime instanceof String s && s.toLowerCase(Locale.ROOT).startsWith("audio/") && nonempty(get(inline, "data"))) return true;
                }
                return false;
            case "bedrock.converse": {
                Object delta = get(get(event, "contentBlockDelta"), "delta");
                Object tool = get(delta, "toolUse");
                return nonempty(get(delta, "text")) || nonempty(get(get(delta, "reasoningContent"), "text")) || nonempty(get(tool, "name")) || arguments(get(tool, "input"))
                    || nonempty(get(get(get(get(event, "contentBlockStart"), "start"), "toolUse"), "name"));
            }
            case "cohere.chat": {
                String type = string(or(get(event, "type"), get(event, "event_type"))); Object message = get(get(event, "delta"), "message");
                if ("content-start".equals(type) || "content-delta".equals(type)) return content(get(message, "content"));
                if ("tool-plan-delta".equals(type)) return nonempty(get(message, "tool_plan"));
                if ("tool-call-start".equals(type) || "tool-call-delta".equals(type)) {
                    for (Object call : objects(get(message, "tool_calls"))) if (function(get(call, "function"))) return true;
                }
                if ("text-generation".equals(type)) return nonempty(get(event, "text"));
                if ("tool-calls-chunk".equals(type)) {
                    Object delta = get(event, "tool_call_delta");
                    return nonempty(get(event, "text")) || nonempty(get(delta, "name")) || arguments(get(delta, "parameters")) || nonempty(get(delta, "text"));
                }
                if ("tool-calls-generation".equals(type)) for (Object call : list(get(event, "tool_calls")))
                    if (nonempty(get(call, "name")) || arguments(get(call, "parameters"))) return true;
                return false;
            }
            case "ollama.chat": {
                if (get(event, "type") != null || get(event, "event_type") != null) return false;
                Object message = get(event, "message");
                if (nonempty(get(message, "content")) || nonempty(get(message, "thinking"))) return true;
                for (Object call : list(get(message, "tool_calls"))) if (function(get(call, "function"))) return true;
                return false;
            }
            case "ollama.generate": return get(event, "type") == null && get(event, "event_type") == null && (nonempty(get(event, "response")) || nonempty(get(event, "thinking")));
            case "cartesia.tts": return "chunk".equals(get(event, "type")) && nonempty(get(event, "data"));
            default: return false;
        }
    }
    private OutputMilestone() { }
}
