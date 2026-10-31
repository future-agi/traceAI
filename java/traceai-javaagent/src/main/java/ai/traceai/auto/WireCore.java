package ai.traceai.auto;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.ToNumberPolicy;

import java.io.InputStream;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Transport-independent port of the shared Python wire core. */
public final class WireCore {
    static final Gson JSON = new GsonBuilder().disableHtmlEscaping().serializeNulls()
        .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create();
    // Positive schema: new provider content fields can never become invocation parameters.
    private static final Set<String> PARAMETER_KEYS = Set.of("model", "model_id", "modelId", "speech_model", "stream", "temperature", "top_p", "topP", "top_k", "topK",
        "frequency_penalty", "presence_penalty", "repeat_penalty", "repetition_penalty", "seed", "max_tokens", "max_completion_tokens", "max_output_tokens", "maxOutputTokens", "maxTokens",
        "num_predict", "num_ctx", "num_batch", "num_gpu", "num_thread", "n", "best_of", "logprobs", "top_logprobs", "echo", "logit_bias", "stop", "stop_sequences", "stopSequences",
        "response_format", "format", "encoding_format", "dimensions", "truncate", "keep_alive", "language", "language_code", "voice", "speed", "sample_rate", "sampleRate",
        "output_format", "optimize_streaming_latency", "top_n", "return_documents", "input_type", "task_type", "taskType", "candidateCount", "include_usage", "type");
    private static final Set<String> PARAMETER_CONTAINERS = Set.of("options", "generationConfig", "generation_config", "inferenceConfig", "textGenerationConfig", "stream_options", "response_format");
    static Map<String, Object> invocationParameters(Map<String, Object> body) { return invocationParameters(body, 0, new int[]{8192}); }
    private static Map<String, Object> invocationParameters(Map<String, Object> body, int depth, int[] budget) {
        Map<String, Object> result = new LinkedHashMap<>();
        if (depth > 16) return result;
        for (var entry : body.entrySet()) {
            if (--budget[0] < 0) break;
            String key = entry.getKey(); Object value = entry.getValue();
            if (PARAMETER_CONTAINERS.contains(key) && value instanceof Map) {
                var nested = invocationParameters(map(value), depth + 1, budget);
                if (!nested.isEmpty()) result.put(key, nested);
            } else if (PARAMETER_KEYS.contains(key)) {
                if (value instanceof String || value instanceof Number || value instanceof Boolean) result.put(key, value);
                else if (Set.of("stop", "stop_sequences", "stopSequences").contains(key) && value instanceof List<?> list) {
                    List<String> strings = new ArrayList<>();
                    for (Object item : list) { if (--budget[0] < 0) break; if (item instanceof String s) strings.add(s); }
                    result.put(key, strings);
                }
            }
        }
        return result;
    }
    private static final Map<String, Object> CATALOG;
    static {
        try (InputStream in = WireCore.class.getResourceAsStream("wire-protocols.json")) {
            CATALOG = map(JSON.fromJson(new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8), Object.class));
        } catch (Exception e) { throw new IllegalStateException("Cannot load wire catalog", e); }
    }

    public record Match(Map<String, Object> endpoint, String provider, String host, int port,
                        Map<String, String> groups, Map<String, String> query) {
        String format() { return str(endpoint.get("format")); }
        boolean catalogProvider() { return list(CATALOG.get("providers")).stream().anyMatch(p -> provider.equals(get(p, "name"))); }
    }
    public record FilePart(String name, String filename, String contentType, long size) {}
    public static final class Request {
        public String method, url;
        public Map<String, String> headers = new LinkedHashMap<>();
        public byte[] body;
        public Map<String, String> fields;
        public List<FilePart> files = new ArrayList<>();
        public long size, fileBytes;
        public boolean truncated, unavailable;
        public int limit = 4194304;
        public String undecoded;
        public Request(String method, String url) { this.method = method; this.url = url; }
    }
    public static final class Response {
        public Integer status;
        public Map<String, String> headers = new LinkedHashMap<>();
        public byte[] body;
        public long size;
        public boolean truncated, incomplete;
        public int limit = 4194304;
        public String undecoded;
        public Double firstChunkSeconds, firstTextSeconds;
        StreamFrames.Timeline arrivals;
        List<StreamFrames.Frame> frames = List.of();
        boolean arrivalLost;
        public Throwable error;
    }
    public record Result(String name, Map<String, Object> attributes, boolean ok, String description) {}
    static final class Message {
        String role, content, toolCallId;
        final List<ToolCall> calls = new ArrayList<>();
        Message(String role, String content) { this.role = role; this.content = content; }
    }
    record ToolCall(String id, String name, String arguments) {}
    static final class Parsed {
        String requestModel, responseModel, responseId, inputValue, inputMime, outputValue, outputMime;
        List<String> finish = new ArrayList<>();
        List<Message> input = new ArrayList<>(), output = new ArrayList<>();
        List<Object> tools = new ArrayList<>();
        Map<String, Object> params = new LinkedHashMap<>(), extra = new LinkedHashMap<>();
        Map<String, Long> usage = new LinkedHashMap<>();
        Map<String, Object> rawUsage = new LinkedHashMap<>();
        String usageFamily;
        boolean skip;
    }
    record Body(String kind, Object json, List<Map<String, Object>> events, String text) {
        boolean streamed() { return kind.equals("sse") || kind.equals("ndjson"); }
    }

    public static Match match(String method, String url) {
        try {
            URI uri = URI.create(url);
            String host = Optional.ofNullable(uri.getHost()).orElse("").toLowerCase(Locale.ROOT);
            int port = uri.getPort() >= 0 ? uri.getPort() : "https".equals(uri.getScheme()) ? 443 : 80;
            String provider = null; int best = Integer.MAX_VALUE;
            for (Object raw : list(CATALOG.get("providers"))) {
                Map<String, Object> p = map(raw);
                List<Boolean> checks = new ArrayList<>(); int rank = Integer.MAX_VALUE;
                String[] rules = {"hosts", "host_suffixes", "host_prefixes", "ports"};
                for (int i = 0; i < rules.length; i++) {
                    if (!p.containsKey(rules[i])) continue;
                    boolean matches = switch (i) {
                        case 0 -> list(p.get(rules[i])).contains(host);
                        case 1 -> list(p.get(rules[i])).stream().anyMatch(s -> host.endsWith(str(s)));
                        case 2 -> list(p.get(rules[i])).stream().anyMatch(s -> host.startsWith(str(s)));
                        default -> list(p.get(rules[i])).stream().anyMatch(n -> n instanceof Number && ((Number)n).intValue() == port);
                    };
                    checks.add(matches); if (matches) rank = Math.min(rank, i);
                }
                if (!checks.isEmpty() && (!"all".equals(p.get("match")) || checks.stream().allMatch(Boolean::booleanValue)) && rank < best) {
                    provider = str(p.get("name")); best = rank;
                }
            }
            String path = uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath();
            for (Object raw : list(CATALOG.get("endpoints"))) {
                Map<String, Object> e = map(raw);
                if (!list(e.get("methods")).contains(method.toUpperCase(Locale.ROOT))) continue;
                String endpointProvider = provider != null ? provider : str(or(e.get("default_provider"), CATALOG.get("default_provider")));
                if (e.containsKey("providers") && !list(e.get("providers")).contains(endpointProvider)) continue;
                if (list(e.get("exclude_providers")).contains(endpointProvider)) continue;
                Matcher matcher = Pattern.compile(str(e.get("path")).replace("?<transcript_id>", "?<transcriptId>")).matcher(path);
                if (!matcher.find()) continue;
                Map<String, String> groups = new LinkedHashMap<>(), query = new LinkedHashMap<>();
                for (String key : List.of("model", "voice", "transcript_id")) {
                    try { String group = matcher.group(key.equals("transcript_id") ? "transcriptId" : key); if (group != null) groups.put(key, decodePath(group)); }
                    catch (IllegalArgumentException ignored) { }
                }
                if (uri.getRawQuery() != null) for (String pair : uri.getRawQuery().split("&")) {
                    String[] kv = pair.split("=", 2);
                    if (kv.length == 2 && !kv[1].isEmpty()) query.putIfAbsent(decode(kv[0]), decode(kv[1]));
                }
                return new Match(e, endpointProvider, host, port, groups, query);
            }
        } catch (RuntimeException ignored) { }
        return null;
    }
    private static String decode(String value) { return URLDecoder.decode(value, StandardCharsets.UTF_8); }
    private static String decodePath(String value) { return decode(value.replace("+", "%2B")); }
    static String rawHeader(Map<String, String> headers, String name) {
        return headers.entrySet().stream().filter(e -> e.getKey().equalsIgnoreCase(name)).map(Map.Entry::getValue).findFirst().orElse("");
    }
    static String contentType(Map<String, String> headers) { return rawHeader(headers, "content-type").split(";", 2)[0].trim().toLowerCase(Locale.ROOT); }
    public static boolean binary(String type) {
        return type.startsWith("audio/") || type.startsWith("video/") || type.startsWith("image/") ||
            type.equals("application/octet-stream") || type.equals("application/vnd.amazon.eventstream");
    }
    @SuppressWarnings("unchecked") static Map<String, Object> map(Object o) { return o instanceof Map ? (Map<String, Object>) o : Collections.emptyMap(); }
    @SuppressWarnings("unchecked") static List<Object> list(Object o) { return o instanceof List ? (List<Object>) o : Collections.emptyList(); }
    static Object get(Object o, String key) { return map(o).get(key); }
    static String str(Object o) { return o == null ? null : String.valueOf(o); }
    static String string(Object o) { return o instanceof String ? (String)o : null; }
    static Object first(Object o) { return list(o).isEmpty() ? null : list(o).get(0); }
    static Object or(Object... objects) { for (Object o : objects) if (truth(o)) return o; return objects.length == 0 ? null : objects[objects.length - 1]; }
    static boolean truth(Object o) { return o != null && !o.equals("") && !o.equals(false) && !(o instanceof Collection && ((Collection<?>)o).isEmpty()) && !(o instanceof Map && ((Map<?, ?>)o).isEmpty()); }
    static String dump(Object o) { return JSON.toJson(o); }
    static String args(Object o) { return o == null ? null : o instanceof String ? (String)o : dump(o); }
    static Object parse(String text) {
        if (text == null || !strictStrings(text)) return null;
        try (var reader = new com.google.gson.stream.JsonReader(new java.io.StringReader(text))) {
            reader.setLenient(false);
            Object value = JSON.getAdapter(Object.class).read(reader);
            return reader.peek() == com.google.gson.stream.JsonToken.END_DOCUMENT ? value : null;
        } catch (java.io.IOException | RuntimeException ignored) { return null; }
    }
    // Gson 2.10's non-lenient reader still permits raw control characters and \\' in strings.
    private static boolean strictStrings(String text) {
        boolean quoted = false;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == '"') { quoted = !quoted; continue; }
            if (!quoted) continue;
            if (c < 0x20) return false;
            if (c != '\\') continue;
            if (++i == text.length()) return false;
            char escape = text.charAt(i);
            if (escape == 'u') {
                for (int k = 0; k < 4; k++) if (++i == text.length() || Character.digit(text.charAt(i), 16) < 0 || text.charAt(i) > 127) return false;
            } else if ("\"\\/bfnrt".indexOf(escape) < 0) return false;
        }
        return !quoted;
    }
    static void put(Map<String, Object> attrs, String key, Object value) {
        if (value == null || value.equals("") || value instanceof List && ((List<?>)value).isEmpty()) return;
        if (value instanceof Map || value instanceof List && !((List<?>)value).stream().allMatch(String.class::isInstance)) value = dump(value);
        attrs.put(key, value);
    }
    static String text(Object content) {
        if (content instanceof String) return (String)content;
        if (content instanceof Map) return get(content, "text") instanceof String ? string(get(content, "text")) : text(get(content, "parts"));
        List<String> texts = new ArrayList<>();
        for (Object part : list(content)) {
            if (part instanceof String) texts.add((String)part);
            else if (get(part, "text") instanceof String && (get(part, "type") == null || List.of("text", "input_text", "output_text").contains(get(part, "type")))) texts.add(string(get(part, "text")));
        }
        return texts.isEmpty() ? null : String.join("\n", texts);
    }
    static Message openaiMessage(Object raw) {
        Message m = new Message(str(or(get(raw, "role"), "user")), text(get(raw, "content")));
        m.toolCallId = string(get(raw, "tool_call_id"));
        for (Object call : list(get(raw, "tool_calls"))) m.calls.add(new ToolCall(string(get(call, "id")), string(get(get(call, "function"), "name")), args(get(get(call, "function"), "arguments"))));
        return m;
    }
    static Message anthropicMessage(String role, Object content) {
        Message m = new Message(role, content instanceof String ? (String)content : null);
        if (content instanceof String) return m;
        List<String> texts = new ArrayList<>();
        for (Object block : list(content)) {
            String kind = string(get(block, "type"));
            if ("text".equals(kind) && get(block, "text") instanceof String) texts.add(string(get(block, "text")));
            else if ("tool_use".equals(kind)) m.calls.add(new ToolCall(string(get(block, "id")), string(get(block, "name")), args(get(block, "input"))));
            else if ("tool_result".equals(kind)) { m.toolCallId = string(get(block, "tool_use_id")); if (truth(text(get(block, "content")))) texts.add(text(get(block, "content"))); }
        }
        m.content = texts.isEmpty() ? null : String.join("\n", texts); return m;
    }
    static Message geminiMessage(Object raw) {
        String role = str(or(get(raw, "role"), "user"));
        Message m = new Message(role.equals("model") ? "assistant" : role, null);
        List<String> texts = new ArrayList<>();
        for (Object part : list(get(raw, "parts"))) {
            if (get(part, "text") instanceof String && !truth(get(part, "thought"))) texts.add(string(get(part, "text")));
            else if (get(part, "functionCall") instanceof Map) { Object c = get(part, "functionCall"); m.calls.add(new ToolCall(string(get(c, "id")), string(get(c, "name")), args(get(c, "args")))); }
            else if (get(part, "functionResponse") instanceof Map) { Object r = get(part, "functionResponse"); m.role = "tool"; m.toolCallId = str(or(get(r, "id"), get(r, "name"))); texts.add(str(or(args(get(r, "response")), ""))); }
        }
        m.content = texts.isEmpty() ? null : String.join("\n", texts); return m;
    }
    static Message bedrockMessage(Object raw) {
        Message m = new Message(str(or(get(raw, "role"), "user")), null);
        List<String> texts = new ArrayList<>();
        for (Object block : list(get(raw, "content"))) {
            if (get(block, "text") instanceof String) texts.add(string(get(block, "text")));
            else if (get(block, "toolUse") instanceof Map) { Object u = get(block, "toolUse"); m.calls.add(new ToolCall(string(get(u, "toolUseId")), string(get(u, "name")), args(get(u, "input")))); }
            else if (get(block, "toolResult") instanceof Map) { Object r = get(block, "toolResult"); m.toolCallId = string(get(r, "toolUseId")); for (Object c : list(get(r, "content"))) if (truth(text(c))) texts.add(text(c)); }
        }
        m.content = texts.isEmpty() ? null : String.join("\n", texts); return m;
    }
    static void commonParams(Map<String, Object> b, Parsed p) {
        for (String key : List.of("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed")) if (b.get(key) instanceof Number) p.params.put(key, b.get(key));
        for (String key : List.of("max_tokens", "max_completion_tokens", "max_output_tokens")) if (b.get(key) instanceof Number) { p.params.put("max_tokens", b.get(key)); break; }
    }
    static void params(Object config, Parsed p, String... pairs) { for (int i = 0; i < pairs.length; i += 2) if (get(config, pairs[i]) instanceof Number) p.params.put(pairs[i + 1], get(config, pairs[i])); }
    static boolean request(Map<String, Object> b, Parsed p, Match m) {
        Object value;
        switch (m.format()) {
            case "openai.chat", "ollama.chat":
                if (b.get("messages") instanceof List) for (Object x : list(b.get("messages"))) { if (x instanceof Map) p.input.add(openaiMessage(x)); }
                else if (m.format().equals("openai.chat") && b.get("prompt") instanceof String) p.input.add(new Message("user", string(b.get("prompt"))));
                else return false;
                p.tools.addAll(list(b.get("tools")));
                if (m.format().equals("ollama.chat")) params(b.get("options"), p, "temperature", "temperature", "top_p", "top_p", "num_predict", "max_tokens", "seed", "seed"); else commonParams(b, p);
                return true;
            case "openai.completions", "openai.images", "ollama.generate":
                value = b.get("prompt");
                if (value == null || !m.format().equals("openai.completions") && !(value instanceof String)) return false;
                if (m.format().equals("ollama.generate") && b.get("system") instanceof String) p.input.add(new Message("system", string(b.get("system"))));
                p.input.add(new Message("user", value instanceof String ? (String)value : String.join("\n", list(value).stream().filter(String.class::isInstance).map(String.class::cast).toList())));
                if (m.format().equals("openai.completions")) commonParams(b, p);
                if (m.format().equals("ollama.generate")) params(b.get("options"), p, "temperature", "temperature", "top_p", "top_p", "num_predict", "max_tokens", "seed", "seed");
                return true;
            case "openai.responses":
                if (!b.containsKey("input") && !b.containsKey("model")) return false;
                if (b.get("instructions") instanceof String) p.input.add(new Message("system", string(b.get("instructions"))));
                value = b.get("input");
                if (value instanceof String) p.input.add(new Message("user", (String)value));
                else for (Object item : list(value)) {
                    if ("function_call_output".equals(get(item, "type"))) { Message msg = new Message("tool", str(or(text(get(item, "output")), args(get(item, "output"))))); msg.toolCallId = string(get(item, "call_id")); p.input.add(msg); }
                    else if ("function_call".equals(get(item, "type"))) { Message msg = new Message("assistant", null); msg.calls.add(new ToolCall(string(get(item, "call_id")), string(get(item, "name")), args(get(item, "arguments")))); p.input.add(msg); }
                    else if (map(item).containsKey("role")) p.input.add(new Message(str(get(item, "role")), text(get(item, "content"))));
                }
                p.tools.addAll(list(b.get("tools"))); commonParams(b, p); return true;
            case "openai.embeddings", "ollama.embed":
                value = b.containsKey("input") ? b.get("input") : m.format().equals("ollama.embed") ? b.get("prompt") : null;
                if (value == null) return false;
                int count = value instanceof List ? list(value).size() : 1;
                if (m.format().equals("openai.embeddings") && (list(value).isEmpty() || first(value) instanceof Number)) count = 1;
                p.extra.put("gen_ai.request.embedding_inputs_count", count); return true;
            case "openai.audio_speech", "deepgram.speak", "elevenlabs.tts", "cartesia.tts":
                String key = m.format().equals("openai.audio_speech") ? "input" : m.format().equals("cartesia.tts") ? "transcript" : "text";
                if (!(b.get(key) instanceof String)) return false;
                p.inputValue = string(b.get(key)); p.inputMime = "text/plain"; p.extra.put("gen_ai.audio.transcript", p.inputValue);
                if (m.format().equals("openai.audio_speech")) put(p.extra, "gen_ai.request.voice", string(b.get("voice")));
                return true;
            case "openai.moderations": return b.get("input") != null;
            case "rerank":
                if (!(b.get("query") instanceof String) || !(b.get("documents") instanceof List)) return false;
                p.input.add(new Message("user", string(b.get("query"))));
                value = b.get("top_n") != null ? b.get("top_n") : b.get("top_k");
                if (value instanceof Number) p.extra.put("gen_ai.request.top_n", ((Number)value).longValue());
                p.extra.put("gen_ai.rerank.documents_count", list(b.get("documents")).size()); return true;
            case "anthropic.messages", "bedrock.invoke":
                if (!(b.get("messages") instanceof List)) return m.format().equals("bedrock.invoke");
                if (truth(b.get("system"))) p.input.add(new Message("system", text(b.get("system"))));
                for (Object raw : list(b.get("messages"))) if (raw instanceof Map) p.input.add(anthropicMessage(str(or(get(raw, "role"), "user")), get(raw, "content")));
                p.tools.addAll(list(b.get("tools"))); commonParams(b, p); return true;
            case "gemini.generate_content":
                value = b.get("contents"); if (value == null) return false;
                Object system = or(b.get("systemInstruction"), b.get("system_instruction")); if (truth(system)) p.input.add(new Message("system", text(system)));
                if (value instanceof String) p.input.add(new Message("user", (String)value));
                else for (Object raw : value instanceof List ? list(value) : List.of(value)) if (raw instanceof Map) p.input.add(geminiMessage(raw));
                p.tools.addAll(list(b.get("tools")));
                params(or(b.get("generationConfig"), b.get("generation_config")), p, "temperature", "temperature", "topP", "top_p", "maxOutputTokens", "max_tokens", "seed", "seed", "frequencyPenalty", "frequency_penalty", "presencePenalty", "presence_penalty"); return true;
            case "gemini.embed_content":
                if (b.containsKey("content")) p.extra.put("gen_ai.request.embedding_inputs_count", 1);
                else if (b.get("requests") instanceof List) p.extra.put("gen_ai.request.embedding_inputs_count", list(b.get("requests")).size()); else return false;
                return true;
            case "cohere.chat":
                if (b.get("messages") instanceof List) for (Object x : list(b.get("messages"))) { if (x instanceof Map) p.input.add(openaiMessage(x)); }
                else if (b.get("message") instanceof String) {
                    if (b.get("preamble") instanceof String) p.input.add(new Message("system", string(b.get("preamble"))));
                    for (Object turn : list(b.get("chat_history"))) p.input.add(new Message(Map.of("USER", "user", "CHATBOT", "assistant", "SYSTEM", "system").getOrDefault(str(or(get(turn, "role"), "")).toUpperCase(Locale.ROOT), "user"), text(get(turn, "message"))));
                    p.input.add(new Message("user", string(b.get("message"))));
                } else return false;
                p.tools.addAll(list(b.get("tools"))); commonParams(b, p); params(b, p, "p", "top_p"); return true;
            case "cohere.embed":
                value = or(b.get("texts"), b.get("inputs"), b.get("images")); if (!(value instanceof List)) return false;
                p.extra.put("gen_ai.request.embedding_inputs_count", list(value).size()); return true;
            case "bedrock.converse":
                if (!(b.get("messages") instanceof List)) return false;
                List<String> systems = list(b.get("system")).stream().map(WireCore::text).filter(WireCore::truth).toList();
                if (!systems.isEmpty()) p.input.add(new Message("system", String.join("\n", systems)));
                for (Object raw : list(b.get("messages"))) if (raw instanceof Map) p.input.add(bedrockMessage(raw));
                params(b.get("inferenceConfig"), p, "temperature", "temperature", "topP", "top_p", "maxTokens", "max_tokens");
                p.tools.addAll(list(get(b.get("toolConfig"), "tools"))); return true;
            default: return true;
        }
    }

    static Parsed check(Match m, Request req) {
        Parsed p = new Parsed();
        String f = m.format();
        boolean multipart = f.equals("openai.audio_transcription") || f.equals("elevenlabs.stt");
        boolean any = f.equals("deepgram.listen") || f.equals("assemblyai.transcript");
        boolean anyJson = f.equals("bedrock.invoke");
        if (req.body != null && req.body.length > req.limit && !rawHeader(req.headers, "content-type").toLowerCase(Locale.ROOT).startsWith("multipart/") && !binary(contentType(req.headers))) { req.truncated = true; req.body = null; }
        if (req.body != null && !req.truncated) {
            EncodedBody.Decoded decoded = EncodedBody.decode(req.body, req.headers, req.limit, false);
            req.body = decoded.bytes(); req.truncated |= decoded.truncated(); req.undecoded = decoded.undecoded();
            req.unavailable |= req.undecoded != null;
        }
        boolean unavailable = req.unavailable || req.truncated && req.fields == null || req.body == null && req.fields == null && !any && !binary(contentType(req.headers));
        if (unavailable) {
            if (!m.catalogProvider()) return null;
            req.unavailable |= !req.truncated;
            return p; // No request-derived content or model when the body cannot be validated.
        }
        if (multipart) {
            if (req.fields == null && req.body != null) parseMultipart(req);
            Map<String, String> fields = req.fields == null ? Map.of() : req.fields;
            String modelKey = f.equals("elevenlabs.stt") ? "model_id" : "model";
            if (!truth(fields.get(modelKey))) return null;
            p.requestModel = fields.get(modelKey);
            put(p.extra, "gen_ai.audio.language", or(fields.get("language"), fields.get("language_code")));
            KnownSecrets.PROCESS.observeBody(fields);
            Map<String, Object> b = new LinkedHashMap<>(fields), params = invocationParameters(b);
            params.remove(modelKey); if (!params.isEmpty()) p.params.put("_json", params);
            for (FilePart file : req.files) { Map<String, Object> info = new LinkedHashMap<>(); info.put("filename", file.filename); info.put("content_type", file.contentType); info.put("bytes", file.size); b.put(file.name, info); }
            p.inputValue = dump(b); p.inputMime = "application/json";
        } else if (req.fields != null || rawHeader(req.headers, "content-type").toLowerCase(Locale.ROOT).startsWith("multipart/")) {
            return null; // Multipart text fields are not JSON validation for an unrelated route.
        } else if (binary(contentType(req.headers))) {
            if (!any) return null;
        } else if (req.body != null) {
            Object raw = parse(new String(req.body, StandardCharsets.UTF_8));
            boolean jsonNull = anyJson && new String(req.body, StandardCharsets.UTF_8).trim().equals("null");
            if (raw == null && !jsonNull && !any && !req.truncated) return null;
            if (raw != null || jsonNull) {
                KnownSecrets.PROCESS.observeBody(raw);
                if (!(raw instanceof Map) && !any && !anyJson || !request(map(raw), p, m)) return null;
                if (p.inputValue == null) { p.inputValue = new String(req.body, StandardCharsets.UTF_8); p.inputMime = "application/json"; }
                for (String key : List.of("model", "model_id", "speech_model", "modelId")) if (get(raw, key) instanceof String) { p.requestModel = string(get(raw, key)); break; }
                Map<String, Object> params = invocationParameters(map(raw));
                if (!params.isEmpty()) p.params.putIfAbsent("_json", params);
            }
        } else if (!any && !req.truncated) return null;
        if (p.requestModel == null) p.requestModel = str(or(m.groups.get("model"), m.query.get("model")));
        if (f.equals("deepgram.listen")) put(p.extra, "gen_ai.audio.language", m.query.get("language"));
        put(p.extra, "gen_ai.request.voice", m.groups.get("voice"));
        if (!req.files.isEmpty()) { FilePart file = req.files.get(0); put(p.extra, "gen_ai.audio.mime_type", file.contentType); p.extra.put("traceai.audio.input_bytes", req.fileBytes > 0 ? req.fileBytes : req.files.stream().mapToLong(FilePart::size).sum()); }
        if (binary(contentType(req.headers))) { p.extra.put("gen_ai.audio.mime_type", contentType(req.headers)); p.extra.put("traceai.audio.input_bytes", req.size > 0 ? req.size : req.body == null ? 0 : req.body.length); }
        return p;
    }
    static void parseMultipart(Request req) {
        BodyCapture capture = new BodyCapture(rawHeader(req.headers, "content-type"), req.limit, false);
        capture.add(java.nio.ByteBuffer.wrap(req.body)); capture.body();
        req.fields = capture.multipart == null ? Map.of() : capture.multipart.fields;
        if (capture.multipart != null) { req.files = capture.multipart.files; req.fileBytes = capture.multipart.fileBytes; }
        req.truncated |= capture.truncated;
    }
    static Body decodeResponse(Response r) {
        String type = contentType(r.headers);
        if (binary(type)) return new Body("binary", null, List.of(), null);
        if (r.body != null && r.body.length > r.limit) r.truncated = true;
        if (r.body == null || r.truncated) return new Body("empty", null, List.of(), null);
        EncodedBody.Decoded decoded = EncodedBody.decode(r.body, r.headers, r.limit, r.error != null || r.incomplete);
        r.truncated |= decoded.truncated(); r.undecoded = decoded.undecoded();
        if (decoded.bytes() == null) return new Body("empty", null, List.of(), null);
        var framed = StreamFrames.decode(decoded.bytes(), type);
        r.frames = framed.frames();
        if (r.arrivals != null && !rawHeader(r.headers, "content-encoding").isBlank() && !rawHeader(r.headers, "content-encoding").equalsIgnoreCase("identity")) r.arrivalLost = true;
        return framed.body();
    }
    static void usage(Parsed p, String key, Object value) { if (value instanceof Number) p.usage.put(key, ((Number)value).longValue()); }
    static void usages(Parsed p, Object raw, String... pairs) { for (int i = 0; i < pairs.length; i += 2) usage(p, pairs[i + 1], get(raw, pairs[i])); }
    static void openaiUsage(Parsed p, Object u) { usageSnapshot(p, "openai", u); }
    static void anthropicUsage(Parsed p, Object u) { usageSnapshot(p, "anthropic", u); }
    static void usageSnapshot(Parsed p, String family, Object snapshot) {
        if (!(snapshot instanceof Map)) return;
        p.usageFamily = family; mergeUsage(p.rawUsage, map(snapshot), 0);
    }
    private static void mergeUsage(Map<String, Object> target, Map<String, Object> snapshot, int depth) {
        if (depth > 32) return;
        snapshot.forEach((key, value) -> {
            if (value instanceof Map) {
                Map<String, Object> nested = target.get(key) instanceof Map ? map(target.get(key)) : new LinkedHashMap<>();
                mergeUsage(nested, map(value), depth + 1); target.put(key, nested);
            } else target.put(key, value); // Explicit null/zero replaces the previous cumulative snapshot too.
        });
    }
    private static Long tokenCount(Map<String, Object> raw, String key) {
        Object value = raw.get(key);
        return value instanceof Long number && number >= 0 ? number : value instanceof Integer number && number >= 0 ? number.longValue() : null;
    }
    private static Long inclusive(Map<String, Object> raw, String base, String... additions) {
        Long result = tokenCount(raw, base); if (result == null) return null;
        try {
            for (String key : additions) { Long value = raw.containsKey(key) ? tokenCount(raw, key) : Long.valueOf(0); if (value == null) return null; result = Math.addExact(result, value); }
        } catch (ArithmeticException ignored) { return null; }
        return result;
    }
    /** A8: normalize once, after every cumulative usage snapshot has merged. Unknown is not zero. */
    static void normalizeUsage(Parsed p) {
        if (p.usageFamily == null) return;
        Map<String, Object> raw = p.rawUsage; Long input, output, total = tokenCount(raw, "total_tokens");
        p.usage.clear();
        switch (p.usageFamily) {
            case "anthropic" -> {
                input = inclusive(raw, "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"); output = tokenCount(raw, "output_tokens");
                usage(p, "cache_read", tokenCount(raw, "cache_read_input_tokens")); usage(p, "cache_write", tokenCount(raw, "cache_creation_input_tokens"));
            }
            case "bedrock.converse" -> {
                input = inclusive(raw, "inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"); output = tokenCount(raw, "outputTokens"); total = tokenCount(raw, "totalTokens");
                usage(p, "cache_read", tokenCount(raw, "cacheReadInputTokens")); usage(p, "cache_write", tokenCount(raw, "cacheWriteInputTokens"));
            }
            case "gemini" -> {
                input = inclusive(raw, "promptTokenCount", "toolUsePromptTokenCount"); output = inclusive(raw, "candidatesTokenCount", "thoughtsTokenCount"); total = tokenCount(raw, "totalTokenCount");
                usage(p, "cache_read", tokenCount(raw, "cachedContentTokenCount")); usage(p, "reasoning", tokenCount(raw, "thoughtsTokenCount"));
            }
            default -> {
                boolean chat = raw.containsKey("prompt_tokens") || raw.containsKey("completion_tokens");
                String ikey = chat ? "prompt_tokens" : "input_tokens", okey = chat ? "completion_tokens" : "output_tokens";
                input = tokenCount(raw, ikey); output = tokenCount(raw, okey);
                Map<String, Object> idetails = map(raw.get(ikey + "_details")), odetails = map(raw.get(okey + "_details"));
                usage(p, "cache_read", tokenCount(idetails, "cached_tokens")); usage(p, "reasoning", tokenCount(odetails, "reasoning_tokens"));
                usage(p, "input_audio", tokenCount(idetails, "audio_tokens")); usage(p, "output_audio", tokenCount(odetails, "audio_tokens"));
            }
        }
        usage(p, "input", input); usage(p, "output", output);
        if (input != null && output != null) {
            try {
                long sum = Math.addExact(input, output);
                if (total != null && total != sum) usage(p, "provider_total", total);
                total = sum;
            } catch (ArithmeticException ignored) { total = null; }
        }
        usage(p, "total", total);
    }
    static void finish(Parsed p, Object reason) { if (truth(reason)) p.finish = new ArrayList<>(List.of(str(reason))); }
    static void transcript(Parsed p, Object text) { if (text instanceof String) { p.extra.put("gen_ai.audio.transcript", text); p.outputValue = (String)text; p.outputMime = "text/plain"; } }
    static void dimension(Parsed p, Object vector) { if (vector instanceof List) p.extra.put("gen_ai.embeddings.dimension.count", list(vector).size()); }
    static void outputJson(Parsed p, Object value) { p.outputValue = dump(value); p.outputMime = "application/json"; }
    static void responsesOutput(Parsed p, Object response) {
        p.responseId = str(or(get(response, "id"), p.responseId)); p.responseModel = str(or(get(response, "model"), p.responseModel));
        Message message = new Message("assistant", null); List<String> texts = new ArrayList<>();
        for (Object item : list(get(response, "output"))) {
            if ("message".equals(get(item, "type"))) { for (Object part : list(get(item, "content"))) if ("output_text".equals(get(part, "type")) && get(part, "text") instanceof String) texts.add(string(get(part, "text"))); }
            else if ("function_call".equals(get(item, "type"))) message.calls.add(new ToolCall(string(get(item, "call_id")), string(get(item, "name")), args(get(item, "arguments"))));
        }
        message.content = texts.isEmpty() ? null : String.join("\n", texts);
        if (message.content != null || !message.calls.isEmpty()) p.output = new ArrayList<>(List.of(message));
        finish(p, get(response, "status"));
    }
    static void openaiChat(Body body, Parsed p) {
        if (body.kind.equals("json") && body.json instanceof Map) {
            p.responseId = string(get(body.json, "id")); p.responseModel = string(get(body.json, "model"));
            for (Object choice : list(get(body.json, "choices"))) {
                if (truth(get(choice, "message"))) p.output.add(openaiMessage(get(choice, "message")));
                else if (get(choice, "text") instanceof String) p.output.add(new Message("assistant", string(get(choice, "text"))));
                if (truth(get(choice, "finish_reason"))) p.finish.add(str(get(choice, "finish_reason")));
            }
            openaiUsage(p, get(body.json, "usage")); return;
        }
        if (!body.kind.equals("sse")) return;
        Map<Integer, Message> messages = new TreeMap<>(); Map<Integer, Map<Integer, Map<String, Object>>> calls = new TreeMap<>(); Map<Integer, String> finishes = new TreeMap<>();
        for (Object chunk : body.events) {
            p.responseId = str(or(p.responseId, get(chunk, "id"))); p.responseModel = str(or(p.responseModel, get(chunk, "model"))); openaiUsage(p, get(chunk, "usage"));
            for (Object choice : list(get(chunk, "choices"))) {
                int index = get(choice, "index") instanceof Number ? ((Number)get(choice, "index")).intValue() : 0;
                Object delta = get(choice, "delta");
                if (truth(get(delta, "role")) || get(delta, "content") instanceof String || get(choice, "text") instanceof String || !list(get(delta, "tool_calls")).isEmpty()) {
                    Message msg = messages.computeIfAbsent(index, i -> new Message("assistant", null));
                    if (truth(get(delta, "role"))) msg.role = str(get(delta, "role"));
                    for (Object v : Arrays.asList(get(delta, "content"), get(choice, "text"))) if (v instanceof String) msg.content = (msg.content == null ? "" : msg.content) + v;
                    for (Object call : list(get(delta, "tool_calls"))) {
                        int k = get(call, "index") instanceof Number ? ((Number)get(call, "index")).intValue() : 0;
                        Map<String, Object> slot = calls.computeIfAbsent(index, i -> new TreeMap<>()).computeIfAbsent(k, i -> new LinkedHashMap<>());
                        if (get(call, "id") != null) slot.putIfAbsent("id", get(call, "id"));
                        if (get(get(call, "function"), "name") != null) slot.putIfAbsent("name", get(get(call, "function"), "name"));
                        if (get(get(call, "function"), "arguments") instanceof String) slot.put("args", str(or(slot.get("args"), "")) + get(get(call, "function"), "arguments"));
                    }
                }
                if (truth(get(choice, "finish_reason"))) finishes.put(index, str(get(choice, "finish_reason")));
            }
        }
        for (Map.Entry<Integer, Message> entry : messages.entrySet()) {
            for (Map<String, Object> slot : calls.getOrDefault(entry.getKey(), Map.of()).values()) entry.getValue().calls.add(new ToolCall(string(slot.get("id")), string(slot.get("name")), string(slot.get("args"))));
            p.output.add(entry.getValue());
        }
        p.finish = new ArrayList<>(finishes.values());
    }
    static void anthropic(Body body, Parsed p) {
        if (body.kind.equals("json") && map(body.json).containsKey("content")) {
            p.responseId = string(get(body.json, "id")); p.responseModel = string(get(body.json, "model"));
            p.output.add(anthropicMessage(str(or(get(body.json, "role"), "assistant")), get(body.json, "content")));
            finish(p, get(body.json, "stop_reason")); anthropicUsage(p, get(body.json, "usage")); return;
        }
        if (!body.kind.equals("sse")) return;
        Map<Integer, Map<String, Object>> blocks = new TreeMap<>();
        for (Object event : body.events) {
            String type = string(get(event, "type"));
            if ("message_start".equals(type)) { Object msg = get(event, "message"); p.responseId = string(get(msg, "id")); p.responseModel = string(get(msg, "model")); anthropicUsage(p, get(msg, "usage")); }
            else if ("content_block_start".equals(type)) { Map<String, Object> block = new LinkedHashMap<>(map(get(event, "content_block"))); block.put("_parts", "text".equals(block.get("type")) ? str(or(block.get("text"), "")) : ""); blocks.put(get(event, "index") instanceof Number ? ((Number)get(event, "index")).intValue() : blocks.size(), block); }
            else if ("content_block_delta".equals(type)) {
                int index = get(event, "index") instanceof Number ? ((Number)get(event, "index")).intValue() : 0;
                Map<String, Object> block = blocks.computeIfAbsent(index, i -> new LinkedHashMap<>(Map.of("type", "text", "_parts", "")));
                Object delta = get(event, "delta"), v = "input_json_delta".equals(get(delta, "type")) ? get(delta, "partial_json") : "text_delta".equals(get(delta, "type")) ? get(delta, "text") : null;
                if (v != null) block.put("_parts", str(block.get("_parts")) + v);
            } else if ("message_delta".equals(type)) { finish(p, get(get(event, "delta"), "stop_reason")); anthropicUsage(p, get(event, "usage")); }
        }
        Message msg = new Message("assistant", null); List<String> texts = new ArrayList<>();
        for (Map<String, Object> block : blocks.values()) {
            if ("text".equals(block.get("type"))) texts.add(str(block.get("_parts")));
            else if ("tool_use".equals(block.get("type"))) msg.calls.add(new ToolCall(string(block.get("id")), string(block.get("name")), str(or(block.get("_parts"), args(block.get("input"))))));
        }
        msg.content = texts.isEmpty() ? null : String.join("\n", texts); if (msg.content != null || !msg.calls.isEmpty()) p.output.add(msg);
    }
    static void cohereStream(Body body, Parsed p) {
        Message message = new Message("assistant", null); StringBuilder text = new StringBuilder();
        Map<Integer, Map<String, String>> calls = new TreeMap<>();
        for (Object event : body.events) {
            String type = str(or(get(event, "type"), get(event, "event_type"), ""));
            Object delta = get(event, "delta");
            switch (type) {
                case "message-start" -> p.responseId = str(or(get(event, "id"), p.responseId));
                case "content-delta" -> { Object value = get(get(get(delta, "message"), "content"), "text"); if (value instanceof String) text.append(value); }
                case "text-generation" -> { if (get(event, "text") instanceof String) text.append(get(event, "text")); }
                case "tool-call-start", "tool-call-delta" -> {
                    int index = get(event, "index") instanceof Number n ? n.intValue() : 0;
                    Map<String, String> slot = calls.computeIfAbsent(index, i -> new LinkedHashMap<>());
                    Object call = get(get(delta, "message"), "tool_calls"), function = get(call, "function");
                    if (get(call, "id") instanceof String id) slot.putIfAbsent("id", id);
                    if (get(function, "name") instanceof String name) slot.putIfAbsent("name", name);
                    if (get(function, "arguments") instanceof String args) slot.merge("args", args, String::concat);
                }
                case "message-end" -> {
                    finish(p, get(delta, "finish_reason"));
                    usages(p, get(get(delta, "usage"), "tokens"), "input_tokens", "input", "output_tokens", "output");
                }
                case "stream-end" -> {
                    Object response = get(event, "response");
                    p.responseId = str(or(get(response, "generation_id"), get(response, "id"), p.responseId));
                    finish(p, or(get(event, "finish_reason"), get(response, "finish_reason")));
                    usages(p, get(get(response, "meta"), "billed_units"), "input_tokens", "input", "output_tokens", "output");
                    if (text.isEmpty() && get(response, "text") instanceof String) text.append(get(response, "text"));
                }
                default -> { }
            }
        }
        for (var slot : calls.values()) message.calls.add(new ToolCall(slot.get("id"), slot.get("name"), slot.get("args")));
        message.content = text.isEmpty() ? null : text.toString();
        if (message.content != null || !message.calls.isEmpty()) p.output.add(message);
    }
    static void response(Body body, Parsed p, Match m) {
        Object data = body.json; String f = m.format();
        switch (f) {
            case "openai.chat", "openai.completions": openaiChat(body, p); break;
            case "openai.responses":
                if (body.kind.equals("json")) { responsesOutput(p, data); openaiUsage(p, get(data, "usage")); }
                else if (body.kind.equals("sse")) {
                    Object last = null; StringBuilder deltas = new StringBuilder();
                    for (Object event : body.events) {
                        String type = str(or(get(event, "type"), ""));
                        openaiUsage(p, get(get(event, "response"), "usage")); openaiUsage(p, get(event, "usage"));
                        if (List.of("response.completed", "response.incomplete", "response.failed").contains(type) && get(event, "response") instanceof Map) last = get(event, "response");
                        else if (type.equals("response.created")) { p.responseId = string(get(get(event, "response"), "id")); p.responseModel = string(get(get(event, "response"), "model")); }
                        else if (type.equals("response.output_text.delta") && get(event, "delta") instanceof String) deltas.append(get(event, "delta"));
                    }
                    if (last != null) responsesOutput(p, last); else if (!deltas.isEmpty()) p.output.add(new Message("assistant", deltas.toString()));
                } break;
            case "openai.embeddings":
                p.responseModel = string(get(data, "model")); dimension(p, get(first(get(data, "data")), "embedding")); openaiUsage(p, get(data, "usage")); break;
            case "openai.audio_transcription":
                if (body.kind.equals("text")) { transcript(p, body.text); break; }
                if (body.kind.equals("sse")) { StringBuilder deltas = new StringBuilder(); Object done = null; for (Object event : body.events) { if ("transcript.text.delta".equals(get(event, "type")) && get(event, "delta") instanceof String) deltas.append(get(event, "delta")); else if ("transcript.text.done".equals(get(event, "type"))) done = event; } data = done != null ? done : Map.of("text", deltas.toString()); }
                transcript(p, get(data, "text")); put(p.extra, "gen_ai.audio.language", string(get(data, "language"))); if (get(data, "duration") instanceof Number) p.extra.put("gen_ai.audio.duration_secs", get(data, "duration"));
                Object u = get(data, "usage"); if ("duration".equals(get(u, "type")) && get(u, "seconds") instanceof Number) p.extra.putIfAbsent("gen_ai.audio.duration_secs", get(u, "seconds")); else openaiUsage(p, u); break;
            case "openai.audio_speech", "deepgram.speak", "elevenlabs.tts", "cartesia.tts":
                if (get(data, "audio_base64") instanceof String) p.extra.put("traceai.audio.output_bytes", base64Size(string(get(data, "audio_base64"))));
                if (body.kind.equals("sse")) { long size = 0; for (Object event : body.events) if (get(event, "data") instanceof String && "chunk".equals(or(get(event, "type"), "chunk"))) size += base64Size(string(get(event, "data"))); p.extra.put("traceai.audio.output_bytes", size); } break;
            case "openai.images":
                if (data instanceof Map) { Object copy = parse(dump(data)); for (Object item : list(get(copy, "data"))) if (map(item).containsKey("b64_json")) map(item).put("b64_json", "<base64 omitted>"); outputJson(p, copy); openaiUsage(p, get(data, "usage")); } break;
            case "openai.moderations":
                if (data instanceof Map) { p.responseId = string(get(data, "id")); p.responseModel = string(get(data, "model")); p.extra.put("traceai.moderation.flagged", list(get(data, "results")).stream().anyMatch(r -> truth(get(r, "flagged")))); outputJson(p, data); } break;
            case "rerank":
                if (data instanceof Map) { p.responseId = string(get(data, "id")); p.responseModel = string(get(data, "model")); List<Object> ranked = new ArrayList<>(); for (Object r : list(or(get(data, "results"), get(data, "data")))) { if (!(r instanceof Map)) continue; Map<String, Object> item = new LinkedHashMap<>(); item.put("index", get(r, "index")); item.put("relevance_score", get(r, "relevance_score")); ranked.add(item); } outputJson(p, ranked); usage(p, "total", get(get(data, "usage"), "total_tokens")); } break;
            case "anthropic.messages": anthropic(body, p); break;
            case "gemini.generate_content":
                Message msg = new Message("assistant", null); StringBuilder texts = new StringBuilder();
                for (Object item : body.kind.equals("sse") ? body.events : data instanceof List ? list(data) : data == null ? List.of() : List.of(data)) {
                    p.responseModel = str(or(get(item, "modelVersion"), p.responseModel)); p.responseId = str(or(get(item, "responseId"), p.responseId));
                    Object candidate = first(get(item, "candidates"));
                    if (candidate instanceof Map) { Message out = geminiMessage(get(candidate, "content")); if (out.content != null) texts.append(out.content); msg.calls.addAll(out.calls); List<String> reasons = new ArrayList<>(); for (Object c : list(get(item, "candidates"))) if (truth(get(c, "finishReason"))) reasons.add(str(get(c, "finishReason"))); if (!reasons.isEmpty()) p.finish = reasons; }
                    usageSnapshot(p, "gemini", get(item, "usageMetadata"));
                }
                msg.content = texts.isEmpty() ? null : texts.toString(); if (msg.content != null || !msg.calls.isEmpty()) p.output.add(msg); break;
            case "gemini.embed_content": dimension(p, get(or(get(data, "embedding"), first(get(data, "embeddings"))), "values")); break;
            case "cohere.chat":
                if (body.streamed()) { cohereStream(body, p); break; }
                if (!(data instanceof Map)) break;
                p.responseId = str(or(get(data, "id"), get(data, "generation_id")));
                if (get(data, "message") instanceof Map) { Message out = openaiMessage(get(data, "message")); if (!map(get(data, "message")).containsKey("role")) out.role = "assistant"; p.output.add(out); }
                else if (get(data, "text") instanceof String) p.output.add(new Message("assistant", string(get(data, "text"))));
                finish(p, get(data, "finish_reason")); Object usage = or(get(data, "usage"), get(data, "meta")); usages(p, or(get(usage, "tokens"), get(usage, "billed_units")), "input_tokens", "input", "output_tokens", "output"); break;
            case "cohere.embed":
                Object embeddings = get(data, "embeddings"); dimension(p, first(embeddings instanceof Map ? get(embeddings, "float") : embeddings)); usage(p, "input", get(get(get(data, "meta"), "billed_units"), "input_tokens")); break;
            case "bedrock.converse":
                Object message = get(get(data, "output"), "message"); if (message instanceof Map) p.output.add(bedrockMessage(message)); finish(p, get(data, "stopReason")); usageSnapshot(p, "bedrock.converse", get(data, "usage")); break;
            case "bedrock.invoke": if (data instanceof Map && map(data).containsKey("content")) anthropic(body, p); else if (body.kind.equals("json")) outputJson(p, data); break;
            case "ollama.chat", "ollama.generate":
                StringBuilder output = new StringBuilder(); Message ollama = new Message("assistant", null);
                for (Object item : body.kind.equals("ndjson") ? body.events : data instanceof Map ? List.of(data) : List.of()) {
                    p.responseModel = str(or(get(item, "model"), p.responseModel));
                    Message delta = openaiMessage(get(item, "message"));
                    if (delta.content != null) output.append(delta.content);
                    ollama.calls.addAll(delta.calls);
                    if (get(item, "response") instanceof String) output.append(get(item, "response"));
                    if (truth(get(item, "done"))) { finish(p, get(item, "done_reason")); usages(p, item, "prompt_eval_count", "input", "eval_count", "output"); }
                }
                ollama.content = output.isEmpty() ? null : output.toString();
                if (ollama.content != null || !ollama.calls.isEmpty()) p.output.add(ollama); break;
            case "ollama.embed": dimension(p, or(first(get(data, "embeddings")), get(data, "embedding"))); usage(p, "input", get(data, "prompt_eval_count")); break;
            case "deepgram.listen":
                Object meta = get(data, "metadata"); p.responseId = string(get(meta, "request_id")); if (get(meta, "duration") instanceof Number) p.extra.put("gen_ai.audio.duration_secs", get(meta, "duration")); transcript(p, get(first(get(first(get(get(data, "results"), "channels")), "alternatives")), "transcript")); break;
            case "elevenlabs.stt": transcript(p, get(data, "text")); put(p.extra, "gen_ai.audio.language", get(data, "language_code")); p.responseId = string(get(data, "transcription_id")); break;
            case "assemblyai.transcript":
                if ("completed".equals(m.endpoint.get("only_when")) && !List.of("completed", "error").contains(str(or(get(data, "status"), "")))) { p.skip = true; break; }
                if (!(data instanceof Map)) break;
                p.responseId = string(get(data, "id")); p.responseModel = string(get(data, "speech_model")); finish(p, get(data, "status")); if ("completed".equals(get(data, "status"))) transcript(p, get(data, "text")); if (get(data, "audio_duration") instanceof Number) p.extra.put("gen_ai.audio.duration_secs", get(data, "audio_duration")); put(p.extra, "gen_ai.audio.language", get(data, "language_code")); break;
            default: break;
        }
    }
    // Compute decoded length without allocating decoded audio.
    static long base64Size(String value) {
        long length = value.chars().filter(c -> c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '+' || c == '/').count();
        return length % 4 == 1 ? 0 : length * 6 / 8;
    }
    static void messages(Map<String, Object> attrs, String prefix, List<Message> messages) {
        for (int i = 0; i < messages.size(); i++) {
            Message msg = messages.get(i); String base = prefix + "." + i + ".message";
            put(attrs, base + ".role", msg.role); put(attrs, base + ".content", msg.content); put(attrs, base + ".tool_call_id", msg.toolCallId);
            for (int k = 0; k < msg.calls.size(); k++) { ToolCall call = msg.calls.get(k); String b = base + ".tool_calls." + k + ".tool_call"; put(attrs, b + ".id", call.id); put(attrs, b + ".function.name", call.name); put(attrs, b + ".function.arguments", call.arguments); }
        }
    }
    public static Result build(Match m, Request req, Response resp) { return build(m, req, resp, true); }
    static Result build(Match m, Request req, Response resp, boolean sanitize) {
        try { return buildChecked(m, req, resp, sanitize); } catch (Throwable ignored) { return null; }
    }
    private static Result buildChecked(Match m, Request req, Response resp, boolean sanitize) {
        Parsed p = check(m, req); if (p == null) return null;
        Body body = decodeResponse(resp); boolean ok = resp.error == null && (resp.status == null || resp.status < 400);
        if (ok || resp.error != null && resp.status != null && resp.status < 400) {
            try { response(body, p, m); } catch (RuntimeException ignored) { }
            if (p.skip && resp.error == null) return null;
        } else if ("completed".equals(m.endpoint.get("only_when")) && resp.error == null) return null;
        Map<String, Object> attrs = new LinkedHashMap<>();
        attrs.put("gen_ai.span.kind", m.endpoint.get("span_kind")); attrs.put("gen_ai.operation.name", m.endpoint.get("operation")); attrs.put("gen_ai.provider.name", m.provider);
        attrs.put("traceai.capture.layer", "wire"); attrs.put("traceai.wire.endpoint", m.endpoint.get("id")); attrs.put("server.address", m.host); attrs.put("server.port", m.port); attrs.put("http.request.method", req.method.toUpperCase(Locale.ROOT));
        put(attrs, "http.response.status_code", resp.status); put(attrs, "gen_ai.request.model", p.requestModel); put(attrs, "gen_ai.response.model", p.responseModel); put(attrs, "gen_ai.response.id", p.responseId); put(attrs, "gen_ai.response.finish_reasons", p.finish);
        for (String key : List.of("temperature", "top_p", "max_tokens", "seed", "frequency_penalty", "presence_penalty")) put(attrs, "gen_ai.request." + key, p.params.get(key));
        put(attrs, "gen_ai.request.parameters", p.params.get("_json"));
        String path = URI.create(req.url).getPath();
        boolean streamingEndpoint = path.endsWith(":streamGenerateContent") || path.endsWith(":streamRawPredict") || path.endsWith("/converse-stream") || path.endsWith("/invoke-with-response-stream");
        if (body.streamed() || streamingEndpoint) {
            attrs.put("gen_ai.request.stream", true);
            if (resp.firstChunkSeconds != null) attrs.put("gen_ai.response.time_to_first_chunk", Math.round(resp.firstChunkSeconds * 1e6) / 1e6);
            if (resp.arrivals != null) {
                if (!resp.arrivalLost) for (var frame : resp.frames) if (OutputMilestone.qualifies(m.format(), frame.value())) {
                    resp.firstTextSeconds = resp.arrivals.at(frame.end());
                    if (resp.firstTextSeconds == null) resp.arrivalLost = true;
                    break;
                }
                if (resp.arrivalLost || resp.arrivals.lost) resp.incomplete = true;
            }
            if (!resp.arrivalLost && resp.firstTextSeconds != null) attrs.put("gen_ai.response.time_to_first_text", Math.round(resp.firstTextSeconds * 1e6) / 1e6);
        }
        normalizeUsage(p);
        Map<String, String> usageKeys = Map.of("input", "input_tokens", "output", "output_tokens", "total", "total_tokens", "cache_read", "cache_read_tokens", "cache_write", "cache_write_tokens", "reasoning", "output_tokens.reasoning", "input_audio", "input_tokens.audio", "output_audio", "output_tokens.audio", "provider_total", "provider_total_tokens");
        if (p.usageFamily == null && !p.usage.containsKey("total") && p.usage.containsKey("input") && p.usage.containsKey("output")) {
            try { p.usage.put("total", Math.addExact(p.usage.get("input"), p.usage.get("output"))); } catch (ArithmeticException ignored) { }
        }
        usageKeys.forEach((k, v) -> put(attrs, "gen_ai.usage." + v, p.usage.get(k)));
        messages(attrs, "gen_ai.input.messages", p.input); messages(attrs, "gen_ai.output.messages", p.output);
        for (int i = 0; i < p.tools.size(); i++) attrs.put("gen_ai.tool.definitions." + i + ".tool.json_schema", dump(p.tools.get(i)));
        if (p.inputValue != null) { attrs.put("input.value", p.inputValue); attrs.put("input.mime_type", or(p.inputMime, "application/json")); }
        if (p.outputValue == null && !p.output.isEmpty()) {
            Message msg = p.output.get(0);
            if (truth(msg.content)) { p.outputValue = msg.content; p.outputMime = "text/plain"; }
            else if (!msg.calls.isEmpty()) {
                List<Object> calls = new ArrayList<>(); for (ToolCall call : msg.calls) { Map<String, Object> item = new LinkedHashMap<>(); item.put("id", call.id); item.put("name", call.name); item.put("arguments", call.arguments); calls.add(item); } outputJson(p, calls);
            }
        }
        if (p.outputValue != null) { attrs.put("output.value", p.outputValue); attrs.put("output.mime_type", or(p.outputMime, "text/plain")); }
        p.extra.forEach((k, v) -> put(attrs, k, v));
        if (body.kind.equals("binary") && ok) { attrs.putIfAbsent("gen_ai.audio.mime_type", contentType(resp.headers)); if (contentType(resp.headers).startsWith("audio/") || "text_to_speech".equals(m.endpoint.get("operation"))) attrs.put("traceai.audio.output_bytes", resp.size); }
        if (req.truncated || resp.truncated) attrs.put("traceai.wire.truncated", true);
        if (req.unavailable) attrs.put("traceai.wire.body_unavailable", true);
        if (resp.incomplete) attrs.put("traceai.wire.incomplete", true);
        put(attrs, "traceai.wire.undecoded_encoding", or(resp.undecoded, req.undecoded));
        String model = str(or(p.requestModel, p.responseModel)), name = str(m.endpoint.get("operation")) + (truth(model) ? " " + model : ""), description = null;
        if (!ok) {
            if (resp.error != null) { attrs.put("error.type", resp.error.getClass().getSimpleName()); description = exceptionMessage(resp.error); }
            else { attrs.put("error.type", String.valueOf(resp.status)); description = errorMessage(body); if (!truth(description)) description = "HTTP " + resp.status; }
        }
        if (!sanitize) return new Result(name, attrs, ok, description);
        KnownSecrets.observe(req.headers, req.url);
        WirePrivacy privacy = new WirePrivacy(Map.of(), TraceAIAuto.config());
        WirePrivacy.Scope scope = privacy.scope(attrs, "", WirePrivacy.Scope.EMPTY);
        attrs.replaceAll((key, value) -> privacy.clean(key, value, scope, true));
        return new Result(privacy.text(name), attrs, ok, privacy.text(description));
    }
    static String exceptionMessage(Throwable error) {
        try {
            if (error.getClass().getMethod("getMessage").getDeclaringClass() == Throwable.class) {
                String message = error.getMessage(); if (message != null && !message.isEmpty()) return message;
            }
        } catch (Throwable ignored) { }
        String name = error.getClass().getSimpleName();
        return name.isEmpty() ? error.getClass().getName() : name;
    }
    static String errorMessage(Body body) {
        Object data = body.json instanceof List ? first(body.json) : body.json, error = get(data, "error");
        if (get(error, "message") instanceof String) return string(get(error, "message")); if (error instanceof String) return (String)error;
        for (String key : List.of("message", "detail", "err_msg")) if (get(data, key) instanceof String) return string(get(data, key));
        if (get(get(data, "detail"), "message") instanceof String) return string(get(get(data, "detail"), "message"));
        return body.text == null ? null : body.text.substring(0, Math.min(500, body.text.length()));
    }
    private WireCore() {}
}
