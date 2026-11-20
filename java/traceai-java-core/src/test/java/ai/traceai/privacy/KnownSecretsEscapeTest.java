package ai.traceai.privacy;

import com.google.gson.Gson;
import com.google.gson.JsonParser;
import com.google.gson.stream.JsonReader;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.StringReader;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * A replaced region never splits a JSON escape: exported JSON always parses, and no secret substring survives in the raw
 * attribute or in the decoded value. Covers the escapes JSON writers emit for control characters, a region that starts or ends
 * inside an escape, and text that was cut at a cap.
 */
class KnownSecretsEscapeTest {
    private static final Gson GSON = new Gson();
    private static final String M = KnownSecrets.TRUNCATION_MARKER;

    private static KnownSecrets.Snapshot snapshot(String... secrets) {
        KnownSecrets registry = new KnownSecrets();
        for (String secret : secrets) registry.register(secret);
        return registry.snapshot();
    }

    /** Strict parse: an illegal escape or a broken structure throws. */
    private static Object parse(String json) {
        try (JsonReader reader = new JsonReader(new StringReader(json))) {
            reader.setLenient(false);
            Object value = JsonParser.parseReader(reader);
            assertThat(reader.peek()).isEqualTo(com.google.gson.stream.JsonToken.END_DOCUMENT);
            return value;
        } catch (Exception e) {
            throw new AssertionError("exported JSON does not parse: " + json, e);
        }
    }

    /** The first illegal JSON escape in text (also in text that is not a whole document), or -1. */
    private static int illegalEscape(String text) {
        for (int i = 0; i < text.length(); i++) {
            if (text.charAt(i) != '\\') continue;
            if (i + 1 >= text.length()) return i;
            char next = text.charAt(++i);
            if ("\"\\/bfnrt".indexOf(next) >= 0) continue;
            if (next != 'u' || i + 4 >= text.length()) return i - 1;
            for (int k = 1; k <= 4; k++) if (Character.digit(text.charAt(i + k), 16) < 0) return i - 1;
            i += 4;
        }
        return -1;
    }

    private static String exported(KnownSecrets.Snapshot snapshot, String value) {
        String json = GSON.toJson(Map.of("v", value));
        String scrubbed = snapshot.scrub(json);
        assertThat(illegalEscape(scrubbed)).as("illegal escape in %s", scrubbed).isEqualTo(-1);
        return scrubbed;
    }

    private static void assertSafe(String scrubbed, String... secrets) {
        Map<?, ?> decoded = GSON.fromJson(scrubbed, Map.class);
        parse(scrubbed);
        for (String secret : secrets) {
            assertThat(scrubbed).doesNotContain(secret);
            assertThat(String.valueOf(decoded.get("v"))).doesNotContain(secret);
        }
    }

    @ParameterizedTest
    @CsvSource({"'\n',nESC-SECRET-VALUE", "'\t',tESC-SECRET-VALUE", "'\r',rESC-SECRET-VALUE", "'\b',bESC-SECRET-VALUE", "'\f',fESC-SECRET-VALUE"})
    void aPartialSecretBeforeAMarkerNeverSplitsAnEscapeLetter(char control, String secret) {
        String scrubbed = exported(snapshot(secret), "text" + control + M);
        assertSafe(scrubbed, secret);
        assertThat(scrubbed).contains(M);
    }

    @ParameterizedTest
    @ValueSource(strings = {"0001-hex-secret-value", "001-hex-secret-value", "01-hex-secret-value", "1-hex-secret-value", "u0001-hex-secret-value"})
    void aPartialSecretBeforeAMarkerNeverSplitsAUnicodeEscape(String secret) {
        assertThat(GSON.toJson(Map.of("v", "text\u0001" + M))).as("the control character is written as a unicode escape before it is replaced").contains("\\u0001");
        String scrubbed = exported(snapshot(secret), "text\u0001" + M);
        assertSafe(scrubbed, secret);
    }

    @ParameterizedTest
    @CsvSource({"'\n',n12345678", "'\t',t12345678", "'\r',r12345678", "'\b',b12345678", "'\f',f12345678"})
    void aFullMatchRightAfterAnEscapeLetterTakesTheWholeEscape(char control, String secret) {
        String scrubbed = exported(snapshot(secret), "x" + control + secret.substring(1) + "y");
        assertSafe(scrubbed, secret);
    }

    @ParameterizedTest
    @ValueSource(strings = {"0001-hex-secret", "001-hex-secret", "01-hex-secret", "1-hex-secret", "u0001-hex-secret"})
    void aFullMatchStartingInsideAUnicodeEscapeTakesTheWholeEscape(String secret) {
        String scrubbed = exported(snapshot(secret), "x\u0001-hex-secret y");
        assertSafe(scrubbed, secret);
    }

    @Test
    void aRegionThatEndsRightAfterAnEscapeBackslashTakesTheEscapedCharacter() {
        // The secret ends with a backslash; in the JSON text the backslash introduces \" , which a replaced region must not leave unpaired.
        String secret = "abcdefgh\\";
        String scrubbed = exported(snapshot(secret), "abcdefgh\"tail");
        assertSafe(scrubbed, "abcdefgh\"");
        parse(scrubbed);
    }

    @Test
    void anEscapedBackslashBeforeALetterIsNotAnEscapeOfThatLetter() {
        // \\n is a backslash followed by the letter n: a secret starting with n is replaced from the n, and the backslash pair stays whole.
        String scrubbed = exported(snapshot("nLETTER-SECRET-1"), "path\\" + "nLETTER-SECRET-1");
        assertSafe(scrubbed, "nLETTER-SECRET-1");
        assertThat(GSON.fromJson(scrubbed, Map.class).get("v")).isEqualTo("path\\" + KnownSecrets.PLACEHOLDER);
    }

    private static boolean hasLoneSurrogate(String text) {
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (Character.isHighSurrogate(c)) {
                if (i + 1 >= text.length() || !Character.isLowSurrogate(text.charAt(i + 1))) return true;
                i++;
            } else if (Character.isLowSurrogate(c)) return true;
        }
        return false;
    }

    /**
     * A registered secret really straddles the cut: the kept text ends with its first {@code k} characters, so its start lies within
     * {@code longest() - 1} of the cut. The sealed text then goes into a JSON string, where a control character before the secret is an
     * escape. No secret character may be left at the cut, in the raw attribute or the decoded value, and the JSON must parse.
     */
    @ParameterizedTest
    @ValueSource(strings = {"QCAPSECRET-VALUE-9988", "nQCAPSECRET-VALUE-9988", "u0001QCAPSECRET-VAL"})
    void textCutInsideASecretLeavesNoPartOfItAtTheCap(String secret) {
        KnownSecrets.Snapshot snapshot = snapshot(secret);
        for (String control : new String[] {"", "\n", "\u0001"}) {
            for (int k = 1; k < secret.length(); k++) {
                String cutText = "x".repeat(300) + control + secret.substring(0, k);
                String sealed = snapshot.sealCut(cutText, 400);
                assertThat(sealed).endsWith(M).hasSizeLessThanOrEqualTo(400);
                String scrubbed = snapshot.scrub(GSON.toJson(Map.of("v", sealed)));
                assertSafe(scrubbed, secret);
                String decoded = String.valueOf(GSON.fromJson(scrubbed, Map.class).get("v"));
                String left = decoded.replace(M, "").replace(KnownSecrets.PLACEHOLDER, "").replace("x", "").replace("\n", "").replace("\u0001", "");
                assertThat(left).as("k=%d control=%s: part of the secret is left at the cut in %s", k, control.length(), scrubbed).isEmpty();
            }
        }
    }

    @Test
    void aFullValueWhoseCutLandsInsideTheSecretIsReplacedFirstEverywhere() {
        KnownSecrets.Snapshot snapshot = snapshot("QCAPSECRET-VALUE-9988");
        for (int offset = 0; offset < 21; offset++) {
            String value = "x".repeat(399 - offset) + "\n" + "QCAPSECRET-VALUE-9988" + "z".repeat(50);
            String capped = snapshot.cap(value, 400);
            String scrubbed = snapshot.scrub(GSON.toJson(Map.of("v", capped)));
            assertSafe(scrubbed, "QCAPSECRET-VALUE-9988");
            assertThat(String.valueOf(GSON.fromJson(scrubbed, Map.class).get("v")).replace(KnownSecrets.PLACEHOLDER, "")).doesNotContain("Q");
        }
    }

    @Test
    void anEscapedSurrogatePairIsOneUnitWhenASecretEndsAfterItsHighHalf() {
        String secret = "ABCDEFGH\\ud83d";
        String json = "{\"v\":\"pre ABCDEFGH\\ud83d\\ude00 post\"}";
        String scrubbed = snapshot(secret).scrub(json);
        assertThat(illegalEscape(scrubbed)).isEqualTo(-1);
        parse(scrubbed);
        String decoded = String.valueOf(GSON.fromJson(scrubbed, Map.class).get("v"));
        assertThat(hasLoneSurrogate(decoded)).as("a lone surrogate in %s", scrubbed).isFalse();
        assertThat(decoded).isEqualTo("pre " + KnownSecrets.PLACEHOLDER + " post");
    }

    @Test
    void anEscapedSurrogatePairIsOneUnitWhenASecretStartsAtItsLowHalf() {
        String secret = "\\ude00TAIL-OF-SECRET";
        String json = "{\"v\":\"pre \\ud83d\\ude00TAIL-OF-SECRET post\"}";
        String scrubbed = snapshot(secret).scrub(json);
        parse(scrubbed);
        String decoded = String.valueOf(GSON.fromJson(scrubbed, Map.class).get("v"));
        assertThat(hasLoneSurrogate(decoded)).as("a lone surrogate in %s", scrubbed).isFalse();
        assertThat(decoded).doesNotContain("TAIL-OF-SECRET");
    }

    @Test
    void aPartialSecretEndingInABackslashBeforeAMarkerKeepsTheWholeMarker() {
        String scrubbed = snapshot("abcdefg\\").scrub("xx abcdefg\\" + M);
        assertThat(scrubbed).isEqualTo("xx " + KnownSecrets.PLACEHOLDER + M);
    }

    @Test
    void anAllBackslashSecretIsReplacedInLinearTime() {
        String secret = "\\".repeat(16);
        KnownSecrets.Snapshot snapshot = snapshot(secret);
        String text = "\\".repeat(200_000) + "end";
        String scrubbed = org.junit.jupiter.api.Assertions.assertTimeoutPreemptively(java.time.Duration.ofSeconds(10), () -> snapshot.scrub(text));
        assertThat(scrubbed).doesNotContain(secret);
    }

    @Test
    void aSealedFieldEndingInARawControlCharacterIsStillValidJsonAfterExportReplacement() {
        // The field Spring writes: sealed text ending in a raw newline before the marker.
        for (String secret : new String[] {"nSEAL-SECRET-VALUE", "tSEAL-SECRET-VALUE"}) {
            String sealed = "line one\n" + M;
            String scrubbed = snapshot(secret).scrub(GSON.toJson(Map.of("v", sealed)));
            assertSafe(scrubbed, secret);
        }
    }
}
