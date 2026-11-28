package ai.traceai.auto;

import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The credential pair rule on the annotation path and on wire free text: a credential-named pair is replaced to the end of its
 * value wherever it sits, the JSON of a structured string is not read a second time, and the rule is linear in the text.
 */
class SharedPairRuleRegressionTest {
    /** Each text holds one credential value that is no part of the text around it. */
    static final Map<String, String> SECRET_TEXTS = new LinkedHashMap<>();
    static {
        SECRET_TEXTS.put("Really??password=SECRETQ1hunter", "SECRETQ1hunter");
        SECRET_TEXTS.put("a?b?token=SECRETQ2value", "SECRETQ2value");
        SECRET_TEXTS.put("see https://x/a#section&password=SECRETQ3hunter", "SECRETQ3hunter");
        SECRET_TEXTS.put("open https://app.example.com/#/reset?token=SECRETQ4abc123 to reset", "SECRETQ4abc123");
        SECRET_TEXTS.put("login?password=SECRETQ5abc#SECRETQ5tail", "SECRETQ5tail");
        SECRET_TEXTS.put("login?password=SECRETQ6abc\"SECRETQ6tail", "SECRETQ6tail");
        SECRET_TEXTS.put("login?password=SECRETQ7abc<SECRETQ7tail", "SECRETQ7tail");
        SECRET_TEXTS.put("login?password=SECRETQ8abc>SECRETQ8tail", "SECRETQ8tail");
        SECRET_TEXTS.put("login?password=SECRETQ9abc\\SECRETQ9tail", "SECRETQ9tail");
        SECRET_TEXTS.put("go password=SECRETQAab#cd\"ef<gh>ij\\SECRETQAtail then", "SECRETQAtail");
        SECRET_TEXTS.put("go login?password=SECRETQBab#cd\"ef<gh>ij\\SECRETQBtail then", "SECRETQBtail");
    }

    @Test void noCredentialValueSurvivesOnTheAnnotationPath() {
        List<String> failures = new ArrayList<>();
        SECRET_TEXTS.forEach((text, secret) -> {
            String captured = String.valueOf(CapturedValue.of(text, 4096));
            if (captured.contains(secret)) failures.add(secret + " in " + captured);
        });
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    @Test void noCredentialValueSurvivesInWireFreeText() {
        List<String> failures = new ArrayList<>();
        SECRET_TEXTS.forEach((text, secret) -> {
            String exported = SharedPairRuleTest.export(Map.of("input.value", text));
            if (exported.contains(secret)) failures.add(secret + " in " + exported);
        });
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    @Test void aValueHoldingHashQuoteAnglesAndBackslashIsReplacedToItsRealEndOnBothPaths() {
        String text = "start password=ab#cd\"ef<gh>ij\\kl-REALEND and more";
        for (String path : List.of("annotation", "wire")) {
            String out = path.equals("wire") ? exported(SharedPairRuleTest.export(Map.of("input.value", text)), "input.value") : String.valueOf(CapturedValue.of(text, 4096));
            for (String piece : List.of("ab", "cd", "ef", "gh", "ij", "kl", "REALEND")) assertFalse(out.contains(piece), path + ": " + piece + " in " + out);
            assertTrue(out.contains("start"), out);
        }
    }

    /** The string the exporter wrote for one attribute. */
    static String exported(String json, String key) {
        Object found = find(WireCore.parse(json), key);
        assertNotNull(found, key + " in " + json);
        return String.valueOf(found);
    }
    private static Object find(Object node, String key) {
        if (node instanceof Map<?, ?> map) {
            if (key.equals(map.get("key")) && map.get("value") instanceof Map<?, ?> value) return value.get("stringValue");
            for (Object child : map.values()) { Object r = find(child, key); if (r != null) return r; }
        } else if (node instanceof List<?> list) for (Object child : list) { Object r = find(child, key); if (r != null) return r; }
        return null;
    }

    @Test void aStructuredStringKeepsItsOtherFieldsAndStaysValidJson() {
        String body = "{\"messages\":[{\"content\":\"please use password=SECRETJ1value\",\"name\":\"alice\",\"role\":\"user\"},{\"content\":\"second note\",\"role\":\"assistant\"}],\"model\":\"gpt-x\"}";
        String out = exported(SharedPairRuleTest.export(Map.of("input.value", body)), "input.value");
        assertFalse(out.contains("SECRETJ1value"), out);
        Object parsed = assertDoesNotThrow(() -> WireCore.parse(out), "valid JSON: " + out);
        assertTrue(parsed instanceof Map, out);
        for (String kept : List.of("\"name\":\"alice\"", "\"role\":\"user\"", "second note", "\"model\":\"gpt-x\"")) assertTrue(out.replace(" ", "").contains(kept.replace(" ", "")), kept + " in " + out);
    }

    @Test void aWireBodyKeepsItsOtherFieldsAfterAMessageHoldingAPair() {
        var request = AdversarialTest.request("https://api.openai.com/v1/chat/completions",
            Map.of("model", "m", "messages", List.of(new LinkedHashMap<>(Map.of("role", "user", "content", "use password=SECRETJ2value")), Map.of("role", "assistant", "content", "second note"))));
        var response = AdversarialTest.response("application/json", "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"ok\"}}]}");
        var attributes = AdversarialTest.build(request, response).attributes();
        String json = WireCore.dump(attributes);
        assertFalse(json.contains("SECRETJ2value"), json);
        assertTrue(json.contains("second note"), json);
        assertTrue(json.contains("gpt") || json.contains("\"m\"") || json.contains("assistant"), json);
        if (attributes.get("input.value") instanceof String text && text.stripLeading().startsWith("{")) assertDoesNotThrow(() -> WireCore.parse(text), "valid JSON: " + text);
    }

    static long fastestMs(String text) {
        CredentialPairs.redactCredentialPairs("warm-up &pwd=a ?a?pwd=b".repeat(50));
        long best = Long.MAX_VALUE;
        for (int run = 0; run < 3; run++) {
            long begin = System.nanoTime();
            CredentialPairs.redactCredentialPairs(text);
            best = Math.min(best, (System.nanoTime() - begin) / 1_000_000);
        }
        return best;
    }

    @Test void queryPairRepetitionsAtTheWireBudgetAreLinear() {
        int budget = 4 * 1024 * 1024;
        for (String unit : List.of("&pwd=", "?a?pwd=", "a?b&pwd=x ")) {
            String text = unit.repeat(budget / unit.length());
            long ms = fastestMs(text);
            assertTrue(ms < 200, unit + " x " + text.length() / unit.length() + " took " + ms + " ms");
        }
    }
}
