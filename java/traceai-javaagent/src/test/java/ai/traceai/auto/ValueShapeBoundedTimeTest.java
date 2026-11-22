package ai.traceai.auto;

import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

/**
 * Value-shape scrubbing must be linear on adversarial repeated starts, and real JWTs must be replaced whole.
 * Goes through the same entry the agent uses: PrivacyCatalog.INSTANCE.scrubValueShapes (via WirePrivacy.credentialText and CapturedValue).
 */
class ValueShapeBoundedTimeTest {
    static final int N = 10_000;          // repeat count for every adversarial input
    static final long BUDGET_MS = 100;     // per input, after one warm-up
    static final String MARK = "<redacted>";

    static String scrub(String text) { return PrivacyCatalog.INSTANCE.scrubValueShapes(text); }

    /** Warms the scrubber once, then asserts the fastest of three scrubs of the input finishes inside the budget. */
    static void bounded(String label, String input) {
        scrub("warm-up eyJ-eyJ-sk-AKIA Bearer -----BEGIN PRIVATE KEY----- x".repeat(50));
        String out = null;
        long ms = Long.MAX_VALUE;
        for (int run = 0; run < 3; run++) { // the fastest of three, so one slow run does not decide
            long begin = System.nanoTime();
            out = scrub(input);
            ms = Math.min(ms, (System.nanoTime() - begin) / 1_000_000);
        }
        System.out.println("TIMING " + label + " length=" + input.length() + " ms=" + ms);
        assertNotNull(out);
        assertTrue(ms < BUDGET_MS, label + " took " + ms + " ms (budget " + BUDGET_MS + " ms) for " + input.length() + " chars");
    }

    @Test void jwtDotFreeRepeatedStartIsBounded() { bounded("jwt eyJ-*N", "eyJ-".repeat(N)); }
    @Test void jwtRepeatedStartThenDotIsBounded() { bounded("jwt eyJ-*N+'.'", "eyJ-".repeat(N) + "."); }
    @Test void jwtRepeatedStartWithTwoSegmentsIsBounded() { bounded("jwt (eyJ-*N+'.')+'.'", "eyJ-".repeat(N) + "." + "A".repeat(20) + "."); }
    @Test void jwtRepeatedStartTwoDotsIsBounded() { bounded("jwt ('eyJ.eyJ-')*5000", "eyJ.eyJ-".repeat(5000)); }
    @Test void providerKeyRepeatedStartIsBounded() { bounded("sk- sk-*N+'!'", "sk-".repeat(N) + "!"); }
    @Test void providerKeyLongRunIsBounded() { bounded("sk- ('sk-'+'a'*19+'!')*N/20", ("sk-" + "a".repeat(19) + "!").repeat(N)); }
    @Test void accessKeyRepeatedStartIsBounded() { bounded("AKIA AKIA*N+'!'", "AKIA".repeat(N) + "!"); }
    @Test void accessKeyAsiaRepeatedStartIsBounded() { bounded("ASIA ASIA*N", "ASIA".repeat(N)); }
    @Test void bearerRepeatedStartIsBounded() { bounded("Bearer 'Bearer '*N", "Bearer ".repeat(N)); }
    @Test void bearerWhitespaceRunIsBounded() { bounded("Bearer 'Bearer'+' '*N*4+'!'", "Bearer" + " ".repeat(N * 4) + "!"); }
    @Test void basicRepeatedStartIsBounded() { bounded("Basic 'Basic '*N", "Basic ".repeat(N)); }
    @Test void privateKeyRepeatedStartIsBounded() { bounded("PEM BEGIN*N", "-----BEGIN PRIVATE KEY-----".repeat(N)); }
    @Test void privateKeyRepeatedStartWithBodyIsBounded() { bounded("PEM (BEGIN+'\\n')*N", "-----BEGIN PRIVATE KEY-----\n".repeat(N)); }

    // Fake JWTs: base64url header/payload (real-looking), made-up signatures.
    static final String HEADER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9";
    static final String PAYLOAD = "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ";
    static final String SIGNATURE = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
    static final String JWT = HEADER + "." + PAYLOAD + "." + SIGNATURE;
    static final String JWT_DASH_END = HEADER + "." + PAYLOAD + "." + "abcdEFGH1234_xyz-";
    static final String JWT_DASH_MID = HEADER + "." + PAYLOAD + "." + "abc-def_GHI-jkl1234";
    static final String JWT_ES = "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJhdXRoLmV4YW1wbGUiLCJleHAiOjQxMDI0NDQ4MDB9.MEUCIQDx-8vK_2pQzLmN4rT7uYwXyZ0aBcD1eFgH2iJkLwIgA";

    @Test void realJwtsAreReplacedWhole() {
        Map<String, String> cases = new LinkedHashMap<>();
        cases.put("bare", JWT);
        cases.put("signature ending in dash", JWT_DASH_END);
        cases.put("dash inside signature", JWT_DASH_MID);
        cases.put("ES256 style", JWT_ES);
        for (var c : cases.entrySet()) {
            String jwt = c.getValue();
            assertEquals(MARK, scrub(jwt), c.getKey() + " bare");
            assertEquals("x-" + MARK, scrub("x-" + jwt), c.getKey() + " preceded by dash");
            assertEquals("Authorization: " + MARK + " end", scrub("Authorization: " + jwt + " end"), c.getKey() + " in text");
            assertEquals("token=" + MARK + ".", scrub("token=" + jwt + "."), c.getKey() + " before period");
            assertEquals("\"" + MARK + "\"", scrub("\"" + jwt + "\""), c.getKey() + " quoted");
            String out = scrub("before " + jwt + " after");
            assertFalse(out.contains(jwt.substring(0, 12)) || out.contains(jwt.substring(jwt.length() - 8)), c.getKey() + " left token fragments: " + out);
        }
    }

    // ---- large real tokens must be redacted whole (the jwt shape is unbounded) -------------------------------------------------
    static String b64(int n) { StringBuilder b = new StringBuilder(); String a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"; for (int i = 0; i < n; i++) b.append(a.charAt((i * 7 + i / 5) % a.length())); return b.toString(); }
    static final String BIG_HEADER = "eyJ" + b64(2048);       // x5c-like ~2 KB header
    static final String BIG_PAYLOAD = "eyJ" + b64(12 * 1024); // 12 KB payload
    static final String BIG_HEADER_JWT = BIG_HEADER + "." + PAYLOAD + "." + SIGNATURE;
    static final String BIG_PAYLOAD_JWT = HEADER + "." + BIG_PAYLOAD + "." + SIGNATURE;

    @Test void jwtWithTwoKilobyteHeaderIsRedactedWhole() { assertEquals(MARK, scrub(BIG_HEADER_JWT)); }
    @Test void jwtWithTwelveKilobytePayloadIsRedactedWhole() { assertEquals(MARK, scrub(BIG_PAYLOAD_JWT)); }
    @Test void bearerJwtWithTwoKilobyteHeaderIsRedactedWhole() { assertEquals("Bearer " + MARK, scrub("Bearer " + BIG_HEADER_JWT)); }
    @Test void bearerJwtWithTwelveKilobytePayloadIsRedactedWhole() { assertEquals("Bearer " + MARK, scrub("Bearer " + BIG_PAYLOAD_JWT)); }

    // ---- differential: the unbounded reference regex R against the catalog ------------------------------------------------------
    @Test void jwtShapeMatchesTheUnboundedReferenceRegexOnRandomInputs() {
        java.util.regex.Pattern reference = java.util.regex.Pattern.compile("\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}");
        String[] pieces = {"eyJ", "eyJaaaaaaaa", "aaaaaaaa", ".", "-", "_", " ", "x"};
        java.util.Random random = new java.util.Random(20240607L);
        int replaced = 0;
        for (int round = 0; round < 20_000; round++) {
            StringBuilder builder = new StringBuilder();
            for (int n = random.nextInt(20) + 1; n > 0; n--) builder.append(pieces[random.nextInt(pieces.length)]);
            String input = builder.toString();
            String expected = reference.matcher(input).replaceAll(java.util.regex.Matcher.quoteReplacement(MARK));
            if (!expected.equals(input)) replaced++;
            assertEquals(expected, scrub(input), input);
        }
        assertTrue(replaced > 50, "the generator must produce replacements: " + replaced);
    }
}
