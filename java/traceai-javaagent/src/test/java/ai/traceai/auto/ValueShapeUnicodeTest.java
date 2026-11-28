package ai.traceai.auto;

import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * The catalog's value shapes are read as the shared reference reads them (Python's re): whitespace is every str.isspace character and
 * the case-insensitive shapes fold case over Unicode. And a JSON value followed by other text is no structured string: its pairs are
 * still replaced. Every input is checked on the free-text and framed export and on the annotation path.
 */
class ValueShapeUnicodeTest {
    static final List<String> SHAPED = List.of(
        "x Bearer\u00a0TAILVTAILV y", "x Bearer\u2028TAILVTAILV y", "x Bearer\u001cTAILVTAILV y", "x Bearer\u0085TAILVTAILV y",
        "x Bearer\u3000TAILVTAILV y", "x Bearer\tTAILVTAILV y", "x Bas\u0131c TAILVTAILV y", "x BAS\u0130C TAILVTAILV y", "x Ba\u017fic TAILVTAILV y",
        "x BEARER TAILVTAILV y", "x Bearer \u212aTAILVTAIL y", "x Bearer TAILVTA\u0131L y", "x Bearer " + "\u017f".repeat(8) + " y",
        "x Bearer " + "\u0130".repeat(8) + " y");

    static List<Object> exports(String text) {
        List<Object> out = new ArrayList<>();
        for (boolean framed : List.of(true, false)) out.add(new WirePrivacy(new KnownSecrets().snapshot(), false).clean("", text, WirePrivacy.Scope.EMPTY, framed));
        out.add(String.valueOf(CapturedValue.of(text, 4096)));
        return out;
    }

    @Test void unicodeWhitespaceAndCaseFoldingInAnAuthorizationValueAsTheReferenceReadsThem() {
        List<String> failures = new ArrayList<>();
        for (String text : SHAPED) for (Object out : exports(text)) if (!"x <redacted> y".equals(out)) failures.add(WireCore.dump(text) + " -> " + WireCore.dump(out));
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }

    @Test void aSchemeWordThatDoesNotFoldToTheShapeIsKept() {
        for (Object out : exports("x Beare\u1e59 TAILVTAILV y")) assertEquals("x Beare\u1e59 TAILVTAILV y", out);
    }

    @Test void aJsonValueFollowedByOtherTextKeepsNoCredentialValue() {
        List<String> failures = new ArrayList<>();
        for (String text : List.of("{} password=TAILV tail", "[1] token: TAILV", "{\"a\":1} trailing password=TAILV", "{\"a\":\"x\"} password=TAILV",
                "{}api_keydata:TAILVTAILV"))
            for (Object out : exports(text)) if (String.valueOf(out).contains("TAILV")) failures.add(text + " -> " + out);
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }
    /** Word boundaries as Python's re reads them: a word character is a Unicode letter or number or '_' (never a combining mark,
     *  a zero-width character or connector punctuation other than '_'). Each input with the reference's export of it. */
    static final String[][] BOUNDARIES = {
        {"x\u00e9Bearer TAILVTAILV y", "x\u00e9Bearer TAILVTAILV y"},
        {"x\u0301Bearer TAILVTAILV y", "x\u0301<redacted> y"},
        {"x\u203fBearer TAILVTAILV y", "x\u203f<redacted> y"},
        {"x\u0663Bearer TAILVTAILV y", "x\u0663Bearer TAILVTAILV y"},
        {"x_Bearer TAILVTAILV y", "x_Bearer TAILVTAILV y"},
        {"x\u200bBearer TAILVTAILV y", "x\u200b<redacted> y"},
        {"AKIATAILVTAILVTAIL\u00e9 y", "AKIATAILVTAILVTAIL\u00e9 y"},
        {"AKIATAILVTAILVTAIL\u0301 y", "AKIATAILVTAILVTAIL\u0301 y"},
        {"x \u00e9AKIATAILVTAILVTAIL y", "x \u00e9AKIATAILVTAILVTAIL y"},
        {"x \u03a9AKIATAILVTAILVTAIL y", "x \u03a9AKIATAILVTAILVTAIL y"},
        {"x sk-TAILVTAILVTAILVTAILVTAILV y", "x <redacted> y"},
        {"x \u00e9sk-TAILVTAILVTAILVTAILVTAILV y", "x \u00e9sk-TAILVTAILVTAILVTAILVTAILV y"},
        {"x \u0308eyJTAILVTAIL.TAILVTAIL.TAILVTAIL y", "x \u0308<redacted> y"},
        {"x \u03b7eyJTAILVTAIL.TAILVTAIL.TAILVTAIL y", "x \u03b7eyJTAILVTAIL.TAILVTAIL.TAILVTAIL y"}
    };

    @Test void wordBoundariesOfTheValueShapesAreThoseOfTheReference() {
        List<String> failures = new ArrayList<>();
        for (String[] item : BOUNDARIES) for (Object out : exports(item[0])) if (!item[1].equals(out)) failures.add(WireCore.dump(item[0]) + " -> " + WireCore.dump(out) + ", the reference: " + WireCore.dump(item[1]));
        assertTrue(failures.isEmpty(), String.join("\n", failures));
    }
}
