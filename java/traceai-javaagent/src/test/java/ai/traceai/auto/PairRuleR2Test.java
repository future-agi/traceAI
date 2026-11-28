package ai.traceai.auto;

import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * Round 2 of the shared pair rule: whitespace in the media hint and in authorization harvesting is Python's str.isspace, and a very long
 * map key with many children stays linear.
 */
class PairRuleR2Test {
    /** Every str.isspace character; Java's trim() and Character.isWhitespace differ from it on several of them. */
    static final String[] SPACES = {"\u0009", "\u000b", "\u000c", "\u001c", "\u001d", "\u001e", "\u001f", "\u0020", "\u0085", "\u00a0", "\u1680", "\u2000", "\u2001", "\u2002", "\u2003", "\u2004", "\u2005", "\u2006", "\u2007", "\u2008", "\u2009", "\u200a", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000"};

    static Object export(String key, Object value) {
        return new WirePrivacy(new KnownSecrets().snapshot(), false).clean(key, value, WirePrivacy.Scope.EMPTY, true);
    }

    /** A media payload under a MIME sibling: the sibling's spaces around the type must not change what is exported. */
    @Test void aMimeHintIsStrippedOfEveryPythonSpace() {
        String payload = "AAAA".repeat(40);
        List<String> failures = new ArrayList<>();
        for (String type : List.of("audio/wav", "image/png")) {
            Map<String, Object> plain = new LinkedHashMap<>(); plain.put("mime_type", type); plain.put("data", payload);
            Object expected = export("part", plain);
            for (String space : SPACES) for (String text : List.of(space + type, type + space + ";x=1", space + type + space)) {
                Map<String, Object> map = new LinkedHashMap<>(); map.put("mime_type", text); map.put("data", payload);
                Object out = export("part", map);
                Object want = new LinkedHashMap<>(expected instanceof Map<?, ?> m ? m : Map.of());
                if (want instanceof Map<?, ?> w && out instanceof Map<?, ?> o && !o.get("data").equals(w.get("data"))) failures.add(WireCore.dump(text) + " -> " + o.get("data") + " expected " + w.get("data"));
            }
        }
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    /** Header Authorization: scheme, a Python space, then the token; a body then echoes the token. */
    @Test void anAuthorizationValueIsSplitAtEveryPythonSpace() {
        List<String> failures = new ArrayList<>();
        for (String space : SPACES) {
            String token = "TAILVTOKEN12345678";
            var registry = new KnownSecrets();
            registry.observeRequest(Map.of("Authorization", "Bearer" + space + token), null);
            String scrubbed = registry.snapshot().scrub("echo " + token + " end");
            if (scrubbed.contains(token)) failures.add("header " + WireCore.dump(space) + " -> " + scrubbed);
            var body = new KnownSecrets();
            body.observeBody(Map.of("authorization", "Bearer" + space + token));
            scrubbed = body.snapshot().scrub("echo " + token + " end");
            if (scrubbed.contains(token)) failures.add("body " + WireCore.dump(space) + " -> " + scrubbed);
            var lead = new KnownSecrets();
            lead.observeBody(Map.of("authorization", space + "Bearer" + space + token));
            scrubbed = lead.snapshot().scrub("echo " + token + " end");
            if (scrubbed.contains(token)) failures.add("leading " + WireCore.dump(space) + " -> " + scrubbed);
        }
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    /** One very long map key with many children: the cost grows with the key once, not once per child. */
    @Test void aLongMapKeyWithManyChildrenIsLinear() {
        long[] ms = new long[2];
        int[] sizes = {50_000, 200_000};
        for (int n = 0; n < 2; n++) {
            Map<String, Object> children = new LinkedHashMap<>();
            for (int i = 0; i < 2000; i++) children.put("c" + i, "v");
            Map<String, Object> root = new LinkedHashMap<>(); root.put("k".repeat(sizes[n]), children);
            long start = System.nanoTime();
            Object out = export("", root);
            ms[n] = (System.nanoTime() - start) / 1_000_000;
            System.out.println("long map key chars=" + sizes[n] + " children=2000 ms=" + ms[n]);
            assertNotNull(out);
        }
        assertTrue(ms[1] <= 2_000, "200K key took " + ms[1] + " ms");
        assertTrue(ms[1] <= Math.max(ms[0], 50) * 12, "200K key took " + ms[1] + " ms against " + ms[0] + " ms for 50K");
    }
}
