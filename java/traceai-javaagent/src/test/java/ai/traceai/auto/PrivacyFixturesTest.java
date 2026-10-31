package ai.traceai.auto;

import org.junit.jupiter.api.Test;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class PrivacyFixturesTest {
    @Test void privacyCatalogAndFixtureBundleMatchSpecByteForByte() throws Exception {
        for (String name : List.of("privacy.json", "privacy-fixtures.json")) try (var in = WirePrivacy.class.getResourceAsStream(name)) {
            assertArrayEquals(Files.readAllBytes(WireFixturesTest.SPEC.resolve(name)), Objects.requireNonNull(in).readAllBytes(), name);
        }
    }
    @Test void everySharedPrivacyFixtureAndIdempotency() throws Exception {
        List<Object> cases;
        try (var in = WirePrivacy.class.getResourceAsStream("privacy-fixtures.json")) {
            cases = WireCore.list(WireCore.get(WireCore.parse(new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8)), "cases"));
        }
        assertTrue(cases.size() >= 4985, "never silently skip fixtures");
        List<String> failures = new ArrayList<>(); int checks = 0;
        for (Object fixture : cases) {
            String name = (String)WireCore.get(fixture, "name"); var input = WireCore.map(WireCore.get(fixture, "input"));
            var registry = new KnownSecrets();
            for (Object secret : WireCore.list(input.get("known_secrets"))) registry.register((String)secret);
            var privacy = new WirePrivacy(registry.snapshot(), false);
            var scope = privacy.scope(WireCore.map(input.get("siblings")), "", WirePrivacy.Scope.EMPTY);
            boolean framed = !"free_text_urls".equals(input.get("site"));
            Object actual = privacy.clean((String)input.get("path"), input.get("value"), scope, framed);
            Object expected = WireCore.get(fixture, "expected");
            if (!WireCore.JSON.toJsonTree(expected).equals(WireCore.JSON.toJsonTree(actual))) failures.add(name + " expected=" + WireCore.dump(expected) + " actual=" + WireCore.dump(actual));
            Object again = privacy.clean((String)input.get("path"), actual, scope, framed);
            if (!WireCore.JSON.toJsonTree(actual).equals(WireCore.JSON.toJsonTree(again))) failures.add(name + " is not idempotent: " + WireCore.dump(again));
            checks += 2;
        }
        System.out.println("S6 privacy fixtures: " + cases.size() + "; recognition/idempotency checks: " + checks + "; failures: " + failures.size());
        assertTrue(failures.isEmpty(), () -> failures.size() + " privacy failures\n" + String.join("\n", failures.subList(0, Math.min(40, failures.size()))));
    }
}
