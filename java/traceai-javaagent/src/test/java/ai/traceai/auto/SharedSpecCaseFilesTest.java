package ai.traceai.auto;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The shared spec's case files of phases 1b, 1c, 1d and 1e r2 (spec/auto-instrumentation/notes): every case input is exported by
 * the agent and compared byte for byte with the bundle's expected value for that case. A case file's own expected value is the
 * bundle's, except where a later phase's "changed" list gives the new one.
 */
class SharedSpecCaseFilesTest {
    static final List<String> FILES = List.of("pair-rule-1b-cases-1122.json", "pair-rule-1c-cases-1122.json", "pair-rule-1d-cases-1122.json",
        "pair-rule-1e-r2-cases-1122.json");

    @Test void everyCaseInputOfThePhaseCaseFilesIsExportedAsTheBundleExpects() throws Exception {
        Map<String, Object> bundle = new HashMap<>();
        for (Object fixture : PrivacyFixturesTest.cases()) bundle.put((String)WireCore.get(fixture, "name"), fixture);
        Map<String, Object> changed = new HashMap<>();
        for (String file : FILES) changed.putAll(WireCore.map(Objects.requireNonNullElse(WireCore.get(read(file), "changed"), Map.of())));
        List<String> failures = new ArrayList<>(); int cases = 0;
        for (String file : FILES) {
            for (Object item : WireCore.list(WireCore.get(read(file), "new_cases"))) {
                cases++;
                String name = (String)WireCore.get(item, "name");
                Object fixture = bundle.get(name);
                if (fixture == null) { failures.add(file + " " + name + ": not in the bundle"); continue; }
                Object expected = WireCore.get(fixture, "expected");
                Object own = changed.containsKey(name) ? WireCore.get(changed.get(name), "expected") : WireCore.get(item, "expected");
                if (!WireCore.dump(own).equals(WireCore.dump(expected))) failures.add(file + " " + name + ": the case file and the bundle differ");
                boolean framed = !"free_text_urls".equals(WireCore.get(item, "site"));
                Object actual = new WirePrivacy(new KnownSecrets().snapshot(), false).clean((String)WireCore.get(item, "path"), WireCore.get(item, "value"), WirePrivacy.Scope.EMPTY, framed);
                if (!WireCore.dump(expected).equals(WireCore.dump(actual))) failures.add(file + " " + name + " expected=" + WireCore.dump(expected) + " actual=" + WireCore.dump(actual));
            }
        }
        System.out.println("shared spec case files: " + FILES.size() + " files, " + cases + " cases, " + failures.size() + " disagreements");
        assertTrue(cases >= 1393, "every case of the four files: " + cases);
        assertTrue(failures.isEmpty(), () -> failures.size() + " disagreements\n" + String.join("\n", failures.subList(0, Math.min(20, failures.size()))));
    }

    static Object read(String file) throws Exception {
        return WireCore.parse(Files.readString(WireFixturesTest.SPEC.resolve(Path.of("notes", file))));
    }
}
