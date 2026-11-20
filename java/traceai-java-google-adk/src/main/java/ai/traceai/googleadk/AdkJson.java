package ai.traceai.googleadk;

import com.google.adk.JsonBaseModel;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;

import java.util.LinkedHashMap;
import java.util.Map;

/** ADK's own JSON form of its request and response objects, as plain maps. */
final class AdkJson {
    private AdkJson() {}

    @SuppressWarnings("unchecked")
    static Map<String, Object> request(LlmRequest request) {
        try {
            Map<String, Object> shape = new LinkedHashMap<>();
            shape.put("model", request.model().orElse(null));
            request.config().ifPresent(config -> shape.put("config", config));
            shape.put("contents", request.contents());
            Object parsed = AdkAttributes.parseJson(JsonBaseModel.getMapper().writeValueAsString(shape));
            return parsed instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> response(LlmResponse response) {
        try {
            Object parsed = AdkAttributes.parseJson(JsonBaseModel.getMapper().writeValueAsString(response));
            return parsed instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    static String json(Object value) {
        try {
            return JsonBaseModel.getMapper().writeValueAsString(value);
        } catch (Exception e) {
            return AdkAttributes.toJson(String.valueOf(value));
        }
    }
}
