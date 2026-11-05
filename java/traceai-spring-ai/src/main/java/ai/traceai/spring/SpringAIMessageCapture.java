package ai.traceai.spring;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.ToolResponseMessage;

/** Per-observation, bounded JSON capture. Never serializes arbitrary metadata, media or model objects. */
final class SpringAIMessageCapture {
    private static final Gson JSON = new Gson();
    private final JsonArray messages = new JsonArray();
    // Reserve closing delimiters up front, including one comma per item/field (conservative for the first).
    private int remaining = SpringAIObservationFilter.MAX_CONTENT_LENGTH - 2;

    boolean add(Message message) {
        if (remaining < 64) return false;
        if (message instanceof ToolResponseMessage tool) {
            for (var response : tool.getResponses()) {
                if (remaining < 64) return false;
                JsonObject item = item(messages);
                string(item, "role", "tool");
                string(item, "tool_call_id", response.id());
                string(item, "name", response.name());
                string(item, "content", response.responseData());
            }
        } else {
            JsonObject item = item(messages);
            string(item, "role", message.getMessageType().getValue());
            if (message instanceof AssistantMessage assistant && assistant.hasToolCalls()
                    && reserve("tool_calls".length() + 6)) {
                JsonArray calls = new JsonArray();
                item.add("tool_calls", calls);
                for (var call : assistant.getToolCalls()) {
                    if (remaining < 64) break;
                    JsonObject encoded = item(calls);
                    string(encoded, "id", call.id());
                    string(encoded, "type", call.type());
                    if (reserve("function".length() + 6)) {
                        JsonObject function = new JsonObject();
                        encoded.add("function", function);
                        string(function, "name", call.name());
                        string(function, "arguments", call.arguments());
                    }
                }
            }
            string(item, "content", message.getText());
        }
        return true;
    }

    String json() {
        return JSON.toJson(messages);
    }

    private JsonObject item(JsonArray array) {
        remaining -= 3; // braces and comma
        JsonObject object = new JsonObject();
        array.add(object);
        return object;
    }

    private boolean reserve(int size) {
        if (remaining < size) return false;
        remaining -= size;
        return true;
    }

    private void string(JsonObject object, String key, String value) {
        if (value == null) return;
        int overhead = key.length() + 4; // quoted key, colon, comma
        int budget = remaining - overhead;
        if (budget < 2) return;
        // Only ever serialize a bounded prefix, even for multi-megabyte content. Find the largest
        // prefix whose escaped JSON fits; cutting the final JSON would produce an invalid payload.
        int low = 0;
        int high = Math.min(value.length(), budget - 2);
        while (low < high) {
            int middle = (low + high + 1) / 2;
            if (JSON.toJson(value.substring(0, middle)).length() <= budget) low = middle;
            else high = middle - 1;
        }
        if (low > 0 && low < value.length() && Character.isHighSurrogate(value.charAt(low - 1))
                && Character.isLowSurrogate(value.charAt(low))) low--;
        String captured = value.substring(0, low);
        remaining -= overhead + JSON.toJson(captured).length();
        object.addProperty(key, captured);
    }
}
