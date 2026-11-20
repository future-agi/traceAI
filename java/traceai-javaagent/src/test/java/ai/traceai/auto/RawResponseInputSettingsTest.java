package ai.traceai.auto;

import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RawResponseInputSettingsTest {
    private static final String PROMPT_SENTINEL = "sentinel-request-prompt-7f3a";

    @Test void whollyDumpedResponseOmitsRequestPromptUnderInputSettings() {
        String url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-text-v14/invoke";
        // Cohere Command on Bedrock returns the request prompt next to the generations.
        Map<String, Object> response = Map.of("id", "r1", "prompt", PROMPT_SENTINEL,
            "generations", List.of(Map.of("id", "g1", "text", "model-answer")));
        var request = AdversarialTest.request(url, Map.of("prompt", PROMPT_SENTINEL));
        var result = AdversarialTest.build(request, AdversarialTest.response("application/json", WireCore.dump(response)));
        assertNotNull(result);
        var match = WireCore.match(request.method, request.url);

        // Control: without a flag the output is still recorded.
        var plain = AutoConfig.resolve(Map.of(), Map.of());
        assertTrue(exported(result, match, plain).contains("model-answer"));

        for (String flag : List.of("hide_inputs", "hide_input_messages", "hide_input_text")) {
            var config = AutoConfig.resolve(Map.of(flag, true), Map.of());
            assertFalse(exported(result, match, config).contains(PROMPT_SENTINEL), flag);
        }
    }

    private static String exported(WireCore.Result result, WireCore.Match match, AutoConfig config) {
        StringBuilder out = new StringBuilder();
        AttributeProjection.sanitize(new LinkedHashMap<>(result.attributes()), config, match).forEach((k, v) -> { if (!TraceAIAuto.hidden(k, match, config)) out.append(k).append('=').append(v).append('\n'); });
        return out.toString();
    }
}
