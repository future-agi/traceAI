package ai.traceai.bedrock;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.JsonPrimitive;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.ContextKey;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.SdkField;
import software.amazon.awssdk.core.SdkPojo;
import software.amazon.awssdk.core.SdkRequest;
import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.core.document.Document;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseRequest;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseResponse;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;
import software.amazon.awssdk.services.bedrockruntime.model.Message;

import java.util.Collection;
import java.util.Map;
import java.util.Set;

/** Shared extraction for the legacy facade and the SDK execution hook. */
final class BedrockTracing {
    static final ContextKey<SdkRequest> WRAPPED_REQUEST = ContextKey.named("traceai-bedrock-request");
    private static final Set<String> REGIONS = Set.of("us", "eu", "apac", "global", "au", "jp");
    private static final Set<String> PROVIDERS = Set.of("anthropic", "amazon", "ai21", "cohere", "meta", "mistral", "stability");
    private final FITracer tracer;

    BedrockTracing(FITracer tracer) {
        this.tracer = tracer;
    }

    Span start(SdkRequest request) {
        boolean converse = request instanceof ConverseRequest;
        String model = converse ? ((ConverseRequest) request).modelId() : ((InvokeModelRequest) request).modelId();
        Span span = tracer.startSpan(converse ? "Bedrock Converse" : "Bedrock Invoke Model", FISpanKind.LLM);
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "aws.bedrock");
        span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, converse ? "chat" : "text_completion");
        span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);
        String provider = modelProvider(model);
        if (provider != null) span.setAttribute("gen_ai.bedrock.model_provider", provider);
        // Telemetry extraction must never prevent an SDK operation from running.
        try {
            if (converse) captureInput(span, (ConverseRequest) request);
            else captureInput(span, (InvokeModelRequest) request, provider);
        } catch (RuntimeException ignored) {
            // Malformed/unknown provider payloads still get a lifecycle span.
        }
        return span;
    }

    void complete(Span span, SdkResponse response) {
        try {
            if (response instanceof ConverseResponse) captureOutput(span, (ConverseResponse) response);
            if (response instanceof InvokeModelResponse) captureOutput(span, (InvokeModelResponse) response);
        } catch (RuntimeException ignored) {
            // A successful application response must not become a tracing error.
        }
        span.setStatus(StatusCode.OK);
    }

    void fail(Span span, Throwable error) {
        tracer.setError(span, error);
    }

    /** Only decode known model/profile resource types; opaque application profiles have no known vendor. */
    static String modelProvider(String id) {
        if (id == null) return null;
        String model = id;
        if (model.startsWith("arn:")) {
            String[] arn = model.split(":", 6);
            if (arn.length != 6 || !arn[2].equals("bedrock")) return null;
            String resource = arn[5];
            if (!resource.startsWith("foundation-model/") && !resource.startsWith("inference-profile/")) return null;
            model = resource.substring(resource.indexOf('/') + 1);
        }
        int dot = model.indexOf('.');
        if (dot < 0) return null;
        if (REGIONS.contains(model.substring(0, dot))) model = model.substring(dot + 1);
        dot = model.indexOf('.');
        if (dot < 0) return null;
        String provider = model.substring(0, dot);
        return PROVIDERS.contains(provider) ? provider : null;
    }

    private void captureInput(Span span, ConverseRequest request) {
        if (request.inferenceConfig() != null) {
            var config = request.inferenceConfig();
            if (config.maxTokens() != null) span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, config.maxTokens().longValue());
            if (config.temperature() != null) span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, config.temperature().doubleValue());
            if (config.topP() != null) span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, config.topP().doubleValue());
        }
        if (tracer.getConfig().isHideInputs()) return;
        JsonArray messages = new JsonArray();
        if (!request.system().isEmpty()) {
            messages.add(message("system", sdkJson(request.system(), 0).getAsJsonArray()));
        }
        for (Message msg : request.messages()) messages.add(message(msg.roleAsString(), sdkJson(msg.content(), 0).getAsJsonArray()));
        setMessages(span, messages, true);
        tracer.setInputValue(span, messagesText(messages));
        if (request.toolConfig() != null) {
            setJson(span, SemanticConventions.GEN_AI_TOOL_DEFINITIONS, sdkJson(request.toolConfig().tools(), 0));
        }
        setJson(span, SemanticConventions.RAW_INPUT, sdkJson(request, 0));
    }

    private void captureOutput(Span span, ConverseResponse response) {
        if (response.usage() != null) {
            var usage = response.usage();
            setCount(span, SemanticConventions.LLM_TOKEN_COUNT_PROMPT, usage.inputTokens());
            setCount(span, SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, usage.outputTokens());
            setCount(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL, usage.totalTokens());
        }
        if (response.stopReasonAsString() != null) {
            span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, response.stopReasonAsString());
        }
        if (tracer.getConfig().isHideOutputs()) return;
        if (response.output() != null && response.output().message() != null) {
            Message msg = response.output().message();
            JsonArray messages = new JsonArray();
            messages.add(message(msg.roleAsString(), sdkJson(msg.content(), 0).getAsJsonArray()));
            setMessages(span, messages, false);
            tracer.setOutputValue(span, messagesText(messages));
        }
        setJson(span, SemanticConventions.RAW_OUTPUT, sdkJson(response, 0));
    }

    private void captureInput(Span span, InvokeModelRequest request, String provider) {
        if (request.body() == null) return;
        String body = request.body().asUtf8String();
        JsonObject json = JsonParser.parseString(body).getAsJsonObject();
        if (json.has("messages")) span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "chat");
        if (tracer.getConfig().isHideInputs()) return;
        setJson(span, SemanticConventions.RAW_INPUT, redactBinary(json));
        if (json.has("messages") && json.get("messages").isJsonArray()) {
            JsonArray messages = redactBinary(json.get("messages")).getAsJsonArray();
            setMessages(span, messages, true);
            tracer.setInputValue(span, messagesText(messages));
        } else if ("amazon".equals(provider) && json.has("inputText")) {
            tracer.setInputValue(span, json.get("inputText").getAsString());
        } else if (json.has("prompt")) {
            tracer.setInputValue(span, json.get("prompt").getAsString());
        } else if (json.has("input") && json.get("input").isJsonPrimitive()) {
            tracer.setInputValue(span, json.get("input").getAsString());
        }
    }

    private void captureOutput(Span span, InvokeModelResponse response) {
        if (response.body() == null) return;
        JsonObject json = JsonParser.parseString(response.body().asUtf8String()).getAsJsonObject();
        if (json.has("usage") && json.get("usage").isJsonObject()) {
            JsonObject usage = json.getAsJsonObject("usage");
            Integer input = integer(usage, "input_tokens");
            Integer output = integer(usage, "output_tokens");
            setCount(span, SemanticConventions.LLM_TOKEN_COUNT_PROMPT, input);
            setCount(span, SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, output);
            if (input != null && output != null) span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, (long) input + output);
        }
        if (json.has("stop_reason") && json.get("stop_reason").isJsonPrimitive()) {
            span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, json.get("stop_reason").getAsString());
        }
        if (tracer.getConfig().isHideOutputs()) return;
        setJson(span, SemanticConventions.RAW_OUTPUT, redactBinary(json));
        if (json.has("content") && json.get("content").isJsonArray()) {
            JsonArray messages = new JsonArray();
            messages.add(message("assistant", redactBinary(json.get("content")).getAsJsonArray()));
            setMessages(span, messages, false);
            tracer.setOutputValue(span, messagesText(messages));
        } else if (json.has("results") && json.getAsJsonArray("results").size() > 0) {
            JsonObject result = json.getAsJsonArray("results").get(0).getAsJsonObject();
            if (result.has("outputText")) tracer.setOutputValue(span, result.get("outputText").getAsString());
        } else {
            for (String key : new String[] {"completion", "output", "generation"}) {
                if (json.has(key) && json.get(key).isJsonPrimitive()) {
                    tracer.setOutputValue(span, json.get(key).getAsString());
                    break;
                }
            }
        }
    }

    private static Integer integer(JsonObject object, String key) {
        return object.has(key) && object.get(key).isJsonPrimitive() ? object.get(key).getAsInt() : null;
    }

    private static void setCount(Span span, String key, Integer value) {
        if (value != null) span.setAttribute(key, value.longValue());
    }

    private void setMessages(Span span, JsonArray messages, boolean input) {
        boolean hidden = input ? tracer.getConfig().isHideInputMessages() : tracer.getConfig().isHideOutputMessages();
        if (!hidden && !messages.isEmpty()) {
            setJson(span, input ? SemanticConventions.LLM_INPUT_MESSAGES : SemanticConventions.LLM_OUTPUT_MESSAGES, messages);
        }
    }

    private static JsonObject message(String role, JsonArray content) {
        JsonObject message = new JsonObject();
        message.addProperty("role", role);
        boolean textOnly = !content.isEmpty();
        StringBuilder text = new StringBuilder();
        for (JsonElement block : content) {
            if (!block.isJsonObject() || !block.getAsJsonObject().has("text")) textOnly = false;
            else text.append(block.getAsJsonObject().get("text").getAsString());
        }
        // Preserve the existing text-only shape; retain structured content for tools/media.
        if (textOnly) message.addProperty("content", text.toString());
        else message.add("content", content);
        return message;
    }

    private static String messagesText(JsonArray messages) {
        StringBuilder text = new StringBuilder();
        for (JsonElement item : messages) {
            JsonElement content = item.getAsJsonObject().get("content");
            StringBuilder part = new StringBuilder();
            if (content != null && content.isJsonPrimitive()) part.append(content.getAsString());
            else if (content != null && content.isJsonArray()) {
                for (JsonElement block : content.getAsJsonArray()) {
                    if (block.isJsonObject() && block.getAsJsonObject().has("text")) part.append(block.getAsJsonObject().get("text").getAsString());
                }
            }
            if (part.length() > 0) {
                if (text.length() > 0) text.append('\n');
                text.append(part);
            }
        }
        return text.toString();
    }

    private static void setJson(Span span, String key, JsonElement value) {
        String json = value.toString();
        // Keep attributes valid JSON even when a large tool payload exceeds the limit.
        span.setAttribute(key, json.length() <= 32000 ? json : "{\"omitted\":true,\"reason\":\"size_limit\"}");
    }

    /** Use the SDK's public model metadata, not Gson reflection or SDK toString(). */
    private static JsonElement sdkJson(Object value, int depth) {
        if (value == null) return JsonNull.INSTANCE;
        if (depth > 32) return omitted();
        if (value instanceof SdkBytes) {
            JsonObject result = omitted();
            result.addProperty("size", ((SdkBytes) value).asByteBuffer().remaining());
            return result;
        }
        if (value instanceof Document) return sdkJson(((Document) value).unwrap(), depth + 1);
        if (value instanceof SdkPojo) {
            JsonObject result = new JsonObject();
            for (SdkField<?> field : ((SdkPojo) value).sdkFields()) {
                Object member = field.getValueOrDefault(value);
                if (member == null || member instanceof Collection && ((Collection<?>) member).isEmpty()) continue;
                String name = field.locationName();
                if (name == null) name = field.memberName();
                result.add(name, sdkJson(member, depth + 1));
            }
            return result;
        }
        if (value instanceof Map) {
            JsonObject result = new JsonObject();
            ((Map<?, ?>) value).forEach((key, member) -> result.add(String.valueOf(key), sdkJson(member, depth + 1)));
            return result;
        }
        if (value instanceof Iterable) {
            JsonArray result = new JsonArray();
            for (Object member : (Iterable<?>) value) result.add(sdkJson(member, depth + 1));
            return result;
        }
        if (value instanceof Number) return new JsonPrimitive((Number) value);
        if (value instanceof Boolean) return new JsonPrimitive((Boolean) value);
        if (value instanceof String || value instanceof Enum) return new JsonPrimitive(value.toString());
        return omitted();
    }

    private static JsonObject omitted() {
        JsonObject result = new JsonObject();
        result.addProperty("omitted", true);
        return result;
    }

    /** Never export inline image/document bytes from provider JSON payloads. */
    private static JsonElement redactBinary(JsonElement value) {
        return redactBinary(value, 0);
    }

    private static JsonElement redactBinary(JsonElement value, int depth) {
        if (depth > 32) return omitted();
        if (value.isJsonArray()) {
            JsonArray result = new JsonArray();
            for (JsonElement member : value.getAsJsonArray()) result.add(redactBinary(member, depth + 1));
            return result;
        }
        if (value.isJsonObject()) {
            JsonObject source = value.getAsJsonObject();
            JsonObject result = new JsonObject();
            boolean base64 = source.has("type") && source.get("type").isJsonPrimitive()
                && source.get("type").getAsString().equals("base64");
            source.entrySet().forEach(entry -> result.add(entry.getKey(),
                entry.getKey().equals("bytes") || base64 && entry.getKey().equals("data")
                    ? omitted() : redactBinary(entry.getValue(), depth + 1)));
            return result;
        }
        return value;
    }
}
