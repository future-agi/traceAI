package ai.traceai.auto;

import java.util.*;
import java.util.function.Predicate;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The shared privacy spec, one test per rule group: each selects the shared bundle's cases by what they cover and checks every one of
 * them (the export and its idempotency) with no expectation of this agent's own. The performance group times adversarial inputs.
 */
class SharedPairRuleGroupsTest {
    static List<String> covers(Object fixture) {
        List<String> tags = new ArrayList<>();
        for (Object tag : WireCore.list(WireCore.get(fixture, "covers"))) tags.add((String)tag);
        return tags;
    }

    /** Checks every shared case that covers one of the tags (a tag ending in ':' matches by prefix) or whose name starts with a prefix. */
    static void group(String label, List<String> tags, List<String> namePrefixes) throws Exception {
        Predicate<Object> selected = fixture -> {
            String name = (String)WireCore.get(fixture, "name");
            for (String prefix : namePrefixes) if (name.startsWith(prefix)) return true;
            for (String tag : covers(fixture)) for (String wanted : tags) if (wanted.endsWith(":") ? tag.startsWith(wanted) : tag.equals(wanted)) return true;
            return false;
        };
        List<String> failures = new ArrayList<>(); int count = 0, failing = 0;
        for (Object fixture : PrivacyFixturesTest.cases()) {
            if (!selected.test(fixture)) continue;
            count++;
            List<String> found = PrivacyFixturesTest.check(fixture);
            if (!found.isEmpty()) failing++;
            failures.addAll(found);
        }
        System.out.println("rule group " + label + ": " + count + " cases, " + failing + " failing");
        assertTrue(count > 0, "the group selects shared cases: " + label);
        int shown = Math.min(20, failures.size());
        assertEquals(0, failing, () -> label + ": " + failures.size() + " failures\n" + String.join("\n", failures.subList(0, shown)));
    }

    @Test void group1PairRuleOnEveryPath() throws Exception {
        group("1 pair rule", List.of("credential-pair-rule"), List.of());
    }

    @Test void group2WholeUrlValuesSkipOnlyTheCatalogSpans() throws Exception {
        group("2 whole URL values", List.of("whole-url-pair-rule", "omission-spans"), List.of());
    }

    @Test void group3UrlCatalogFragmentSecondQuestionAndPathPairs() throws Exception {
        group("3 URL catalog", List.of("url-fragment-pairs", "url-path-pairs", "credential-like-host"), List.of("pair-rule-url-", "pair-rule-prose-fragment", "pair-rule-prose-second"));
    }

    @Test void group4VocabularyAtEverySite() throws Exception {
        group("4 vocabulary", List.of("vocabulary:"), List.of());
    }

    @Test void group5JsonQuotedNames() throws Exception {
        group("5 JSON quoted names", List.of("json-quoted-name-ruling", "quoted-name-spellings"), List.of());
    }

    @Test void group6MapKeys() throws Exception {
        group("6 map keys", List.of("map-keys"), List.of());
    }

    @Test void group7DepthBudget() throws Exception {
        group("7 depth", List.of("name-path-budget"), List.of());
    }

    /** A name of more than limits.max_depth components, wherever it is read (a --name past its window, a URL path pair, a pair after
     *  a '#' or a second '?'), exports the whole value as the placeholder. */
    @Test void group7DepthBudgetOnEveryNameSite() {
        String deep = "a.".repeat(40) + "x=TAILV";
        List<String> inputs = List.of("run --" + "a".repeat(100) + ".b".repeat(40) + " TAILV then", "https://h.example/" + deep,
            "https://h.example/p#" + deep, "https://h.example/p?q=1?" + deep, "see https://h.example/" + deep + " here");
        List<String> failures = new ArrayList<>();
        for (String input : inputs) for (boolean framed : List.of(true, false)) {
            Object out = new WirePrivacy(new KnownSecrets().snapshot(), false).clean("", input, WirePrivacy.Scope.EMPTY, framed);
            if (!"<redacted>".equals(out)) failures.add((framed ? "framed " : "free text ") + input.substring(0, 30) + " -> " + out);
        }
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    @Test void group8BoundsFailClosed() throws Exception {
        group("8 bounds", List.of("bounded-names"), List.of());
    }

    @Test void group10NameToSeparatorGaps() throws Exception {
        group("10 gaps", List.of("pair-name-gaps", "encoded-gaps"), List.of());
    }

    @Test void group11PathSegmentsAndEncodedEquals() throws Exception {
        group("11 path segments and encoded equals", List.of("url-path-every-pair"), List.of("pair-rule-1c-data"));
    }

    @Test void group12NameReadBack() throws Exception {
        group("12 name read-back", List.of("encoded-name-read-back", "escaped-name-decode"), List.of());
    }

    @Test void group13DashedNamesAndSingleDashFlags() throws Exception {
        group("13 dashed names", List.of("dashes-gap-whitespace", "dashes-line-break", "dashes-colon-value", "dashes-encoded-gap", "dashes-encoded-control",
            "dashes-word-separator", "single-dash-flags"), List.of());
    }

    @Test void group14QuotedNamesWithEquals() throws Exception {
        group("14 quoted names with equals", List.of("quoted-name-equals"), List.of());
    }

    @Test void group15DataUriContinuationLines() throws Exception {
        group("15 data URI continuation lines", List.of("data-uri-continuation"), List.of());
    }

    /**
     * Group 9: the pair rule and the free-text export are linear on adversarial inputs. Each unit is repeated to 1.6M characters; the
     * pair rule over it must finish under the bound, and it must replace a value exactly where the shared reference does (its timing
     * record lists, for each input, whether the reference changes it).
     */
    static final int SIZE = 1_600_000;
    static final long BOUND_MS = 10_000;
    static final Object[][] UNITS = {
        {"lb-word", "--token\nv ", true}, {"lb-pair", "--token\ntoken=v ", true}, {"lb-blank", "--token\n\nv ", false},
        {"lb-quoted", "--token\n\"" + "a".repeat(120) + "\" ", true}, {"lb-escapes", "--token\n" + "%41".repeat(40) + "=v ", true},
        {"lb-flag", "--token\n--token\n", false}, {"single-dash", "-token v ", true}, {"single-dash-other", "-abc v -p w ", false},
        {"quoted-eq", "\"token\"=v ", true}, {"quoted-eq-control", "\"model\"=v ", false}, {"quoted-eq-encoded", "'pwd'%3Dv, ", true},
        {"word-separators", "--token a=b:c%3Dd ", true}, {"questions", "a?b?c?d?e?f?g?h?i?j=", false}, {"equals", "=".repeat(64) + " ", false},
        {"long-names", "x".repeat(300) + "=v ", true},
    };

    static String repeat(String unit) {
        StringBuilder out = new StringBuilder(SIZE + unit.length());
        while (out.length() < SIZE) out.append(unit);
        out.setLength(SIZE);
        return out.toString();
    }

    /** Group 9 on the export: a data URI followed by one long line of '=' is read once (the padding read is linear in the line). */
    @Test void group9DataUriPaddingLineIsLinear() {
        String text = "data:,x\n" + "=".repeat(SIZE);
        long start = System.nanoTime();
        Object out = new WirePrivacy(new KnownSecrets().snapshot(), false).clean("gen_ai.prompt.0.content", text, WirePrivacy.Scope.EMPTY, false);
        long millis = (System.nanoTime() - start) / 1_000_000;
        System.out.println("export timing data-uri-padding-line chars=" + text.length() + " ms=" + millis);
        assertNotNull(out);
        assertTrue(millis <= BOUND_MS, "took " + millis + " ms");
    }

    @Test void group9LinearOnAdversarialInputs() {
        List<String> failures = new ArrayList<>();
        List<Object[]> inputs = new ArrayList<>(Arrays.asList(UNITS));
        inputs.add(new Object[] {"one-indent-run", "--token\n" + " ".repeat(SIZE - 9) + "v", true});
        inputs.add(new Object[] {"one-word-run", "--token\n" + "a".repeat(SIZE - 8), true});
        inputs.add(new Object[] {"one-quote-run", "\"" + "a".repeat(SIZE - 4) + "\"=v", true});
        CredentialPairs.redactCredentialPairs(repeat("--token\nv "));   // warm-up
        for (Object[] input : inputs) {
            String label = (String)input[0], text = ((String)input[1]).length() >= SIZE - 9 ? (String)input[1] : repeat((String)input[1]);
            long start = System.nanoTime();
            String out = CredentialPairs.redactCredentialPairs(text);
            long millis = (System.nanoTime() - start) / 1_000_000;
            System.out.println("pair rule timing " + label + " chars=" + text.length() + " ms=" + millis + " changed=" + !out.equals(text));
            if (millis > BOUND_MS) failures.add(label + " took " + millis + " ms");
            if (!out.equals(text) != (Boolean)input[2]) failures.add(label + " changed=" + !out.equals(text) + ", the reference: " + input[2]);
        }
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }
}
