package ai.traceai.auto;

import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * Known secrets and the catalog's value shapes write the placeholder before the pair rule runs, so a placeholder in front of
 * the rule is no sign that the rest of the value is gone: the pair rule replaces a credential-named pair to its true end.
 */
class SharedPairRuleSecretTailTest {
    static final String AWS = "AKIAIOSFODNN7EXAMPLE";
    static final String SK = "sk-abcdefghijklmnopqrstuvwx1234";
    static final Map<String, String> TAILS = new LinkedHashMap<>();
    static {
        TAILS.put("password=" + AWS + ",hunter2TAILA", "hunter2TAILA");
        TAILS.put("token=" + SK + " and the old one hunter2TAILB", "hunter2TAILB");
        TAILS.put("aws_credentials=" + AWS + " wJalrXUtnFEMI/K7MDENG/bPxRfiCYTAILC", "bPxRfiCYTAILC");
        TAILS.put("password=<redacted>#hunter2TAILD", "hunter2TAILD");
        TAILS.put("password=<redacted>&hunter2TAILE", "hunter2TAILE");
        TAILS.put("password=<redacted>\"hunter2TAILF", "hunter2TAILF");
        TAILS.put("password=<redacted>abchunter2TAILG", "hunter2TAILG");
    }

    @Test void noSecretTailSurvivesOnTheAnnotationPath() {
        List<String> failures = new ArrayList<>();
        TAILS.forEach((text, tail) -> {
            String captured = String.valueOf(CapturedValue.of(text, 4096));
            if (captured.contains(tail)) failures.add(tail + " in " + captured);
        });
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    @Test void noSecretTailSurvivesInWireFreeText() {
        List<String> failures = new ArrayList<>();
        TAILS.forEach((text, tail) -> {
            String out = SharedPairRuleRegressionTest.exported(SharedPairRuleTest.export(Map.of("input.value", text)), "input.value");
            if (out.contains(tail)) failures.add(tail + " in " + out);
        });
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    @Test void noTailOfAKnownSecretValueSurvives() {
        var registry = new KnownSecrets();
        registry.register("known-secret-value-0001");
        String out = CredentialPairs.scrub(registry.snapshot(), "password=known-secret-value-0001,hunter2TAILH and more");
        assertFalse(out.contains("hunter2TAILH"), out);
    }
}
