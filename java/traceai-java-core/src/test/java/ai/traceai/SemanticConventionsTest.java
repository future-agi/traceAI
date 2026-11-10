package ai.traceai;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

import static org.junit.jupiter.api.Assertions.*;

class SemanticConventionsTest {

    @ParameterizedTest(name = "{0} remains an alias of {1}")
    @CsvSource({
        "LLM_SYSTEM, GEN_AI_PROVIDER_NAME, gen_ai.provider.name",
        "LLM_PROVIDER, GEN_AI_PROVIDER_NAME, gen_ai.provider.name",
        "LLM_MODEL_NAME, GEN_AI_REQUEST_MODEL, gen_ai.request.model",
        "LLM_REQUEST_MODEL, GEN_AI_REQUEST_MODEL, gen_ai.request.model",
        "LLM_REQUEST_TEMPERATURE, GEN_AI_REQUEST_TEMPERATURE, gen_ai.request.temperature",
        "LLM_REQUEST_TOP_P, GEN_AI_REQUEST_TOP_P, gen_ai.request.top_p",
        "LLM_REQUEST_MAX_TOKENS, GEN_AI_REQUEST_MAX_TOKENS, gen_ai.request.max_tokens",
        "LLM_REQUEST_STOP_SEQUENCES, GEN_AI_REQUEST_STOP_SEQUENCES, gen_ai.request.stop_sequences",
        "LLM_RESPONSE_MODEL, GEN_AI_RESPONSE_MODEL, gen_ai.response.model",
        "LLM_RESPONSE_ID, GEN_AI_RESPONSE_ID, gen_ai.response.id",
        "LLM_RESPONSE_FINISH_REASON, GEN_AI_RESPONSE_FINISH_REASONS, gen_ai.response.finish_reasons",
        "LLM_TOKEN_COUNT_PROMPT, GEN_AI_USAGE_INPUT_TOKENS, gen_ai.usage.input_tokens",
        "LLM_TOKEN_COUNT_COMPLETION, GEN_AI_USAGE_OUTPUT_TOKENS, gen_ai.usage.output_tokens",
        "LLM_TOKEN_COUNT_TOTAL, GEN_AI_USAGE_TOTAL_TOKENS, gen_ai.usage.total_tokens",
        "LLM_INPUT_MESSAGES, GEN_AI_INPUT_MESSAGES, gen_ai.input.messages",
        "LLM_OUTPUT_MESSAGES, GEN_AI_OUTPUT_MESSAGES, gen_ai.output.messages"
    })
    void legacyAliasesKeepTheirValuesAndAreDeprecated(String aliasName, String canonicalName,
                                                     String literal) throws ReflectiveOperationException {
        Field alias = SemanticConventions.class.getField(aliasName);
        Field canonical = SemanticConventions.class.getField(canonicalName);
        assertTrue(Modifier.isStatic(alias.getModifiers()));
        assertTrue(Modifier.isFinal(alias.getModifiers()));
        assertEquals(String.class, alias.getType());
        assertTrue(alias.isAnnotationPresent(Deprecated.class), aliasName);
        assertFalse(canonical.isAnnotationPresent(Deprecated.class), canonicalName);
        assertEquals(literal, alias.get(null), aliasName);
        assertEquals(literal, canonical.get(null), canonicalName);
    }

    @Test
    void everyExportedGenAiValueHasThePythonCanonicalName() throws Exception {
        Map<String, String> pythonNames = pythonNamesByValue();
        assertEquals("GEN_AI_PROVIDER_NAME", pythonNames.get("gen_ai.provider.name"));
        assertEquals("GEN_AI_RESPONSE_FINISH_REASONS", pythonNames.get("gen_ai.response.finish_reasons"));

        for (Field field : SemanticConventions.class.getFields()) {
            if (field.getType() != String.class || !Modifier.isStatic(field.getModifiers())) {
                continue;
            }
            String value = (String) field.get(null);
            if (!value.startsWith("gen_ai.")) {
                continue;
            }
            // Java-only attributes use the same dotted-path-to-upper-snake-case rule.
            String name = pythonNames.getOrDefault(value, value.toUpperCase(Locale.ROOT).replace('.', '_'));
            Field canonical = SemanticConventions.class.getField(name);
            assertTrue(Modifier.isStatic(canonical.getModifiers()), name);
            assertTrue(Modifier.isFinal(canonical.getModifiers()), name);
            assertEquals(String.class, canonical.getType(), name);
            assertEquals(value, canonical.get(null), field.getName());
            if (!field.getName().equals(name)) {
                assertTrue(field.isAnnotationPresent(Deprecated.class), field.getName());
            }
        }

        // Check the reverse direction too: an existing canonical name must not drift
        // to a Java-only value and thereby evade the lookup above. Python-only values
        // are not added to Java by this naming-only change.
        for (Map.Entry<String, String> entry : pythonNames.entrySet()) {
            try {
                Field field = SemanticConventions.class.getField(entry.getValue());
                assertEquals(entry.getKey(), field.get(null), entry.getValue());
            } catch (NoSuchFieldException ignored) {
                // This SDK does not yet export this Python attribute value.
            }
        }
    }

    private static Map<String, String> pythonNamesByValue() throws IOException {
        // Locate the checked-out reference instead of maintaining a stale fixture.
        // Supports both `cd java && mvn verify` and `mvn -f java/pom.xml verify`.
        Path root = Path.of("").toAbsolutePath();
        Path relative = Path.of("python", "fi_instrumentation", "fi_types.py");
        while (root != null && !Files.isRegularFile(root.resolve(relative))) {
            root = root.getParent();
        }
        assertNotNull(root, "Run this test from the traceAI checkout containing python/fi_instrumentation/fi_types.py");
        Pattern assignment = Pattern.compile("^    (GEN_AI_\\w+) = \"(gen_ai\\.[^\"]+)\"", Pattern.MULTILINE);
        var matcher = assignment.matcher(Files.readString(root.resolve(relative)));
        Map<String, String> names = new HashMap<>();
        while (matcher.find()) {
            assertNull(names.put(matcher.group(2), matcher.group(1)), "Duplicate Python gen_ai value");
        }
        assertFalse(names.isEmpty(), "Python reference must contain GEN_AI_* constants");
        return names;
    }
}
