package ai.traceai.auto;

import java.util.HashMap;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The credential pair rule of the export privacy pipeline. Every string that is exported passes known-secret replacement, then the
 * catalog's value shapes, then this rule ({@link #scrub}); annotation captures use the same function as every other integration.
 *
 * <p>The rule follows the shared spec's executable reference ({@code spec/auto-instrumentation/generate_privacy_fixtures.py},
 * {@code Reference.credential_pairs}). Space is read three ways, as there: {@link #pySpace} (Python's {@code str.isspace}) for name gaps,
 * --name gaps and --name value words; {@link Character#isWhitespace} where the reference uses Java's whitespace (the gap before a JSON
 * value, the user information word); and plain U+0020 for a readable gap.
 */
final class CredentialPairs {
    private CredentialPairs() { }

    /** The order on every string path: known secrets, then the catalog's credential shapes, then the pair rule. A shape (a Basic or
     *  Bearer value, a key, a JWT) must be matched before the pair rule removes the word that anchors it. */
    static String scrub(KnownSecrets.Snapshot secrets, String text) {
        String shaped = PrivacyCatalog.INSTANCE.scrubValueShapes(secrets.scrub(text));
        return enabled ? redactCredentialPairs(shaped) : shaped;
    }

    private static volatile boolean enabled = true;
    /** Only tests call this, to compare an export with and without the pair rule; nothing in the agent does. */
    static void setEnabledForTests(boolean value) { enabled = value; }
    /** Whether the pair rule runs: the whole-URL path of {@link WirePrivacy} asks before it runs the rule with the catalog's spans. */
    static boolean active() { return enabled; }

    /**
     * Whether a name, as a parameter, a record component, a Map key or a name in text, is a credential name. It is the export catalog's
     * rule, which also knows the vocabulary words ({@code pwd}, {@code pass}, {@code passphrase}, {@code passwd}, {@code passcode},
     * {@code pin}) and cookie names as a whole name or a segment ({@code db.password}, {@code dbPass}), here with the command-line and
     * bracket spellings undone first: {@code --password}, {@code -Ddb.password} and {@code user[password]}. Over-redaction fails closed.
     * A name of more than {@code limits.max_depth} components throws, and the exported value becomes the placeholder.
     */
    static boolean credentialName(String name) {
        if (name == null || name.isEmpty()) return false;
        int dashes = 0;
        while (dashes < name.length() && name.charAt(dashes) == '-') dashes++;
        String stripped = name.substring(dashes);
        if (stripped.startsWith("D") && stripped.length() > 1 && name.startsWith("-D")) stripped = stripped.substring(1);
        stripped = stripped.replace('[', '.').replace(']', '.');
        return !stripped.isEmpty() && (WirePrivacy.credentialKey(stripped) || WirePrivacy.credentialKey(name));
    }

    // ---- Character classes and constants (the reference's module constants) ----

    static final int NAME_CAP = 128, GAP_CAP = 8, QUOTED_NAME_WINDOW = 130;
    static final String ESCAPED_QUOTE = "\\\"", ENTITY_QUOTE = "&quot;", REDACTED = "<redacted>";
    private static final Pattern JSON_NUMBER = Pattern.compile("-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?");
    private static final Pattern PERCENT_ESCAPE = Pattern.compile("%[0-9A-Fa-f]{2}");

    /** Python's {@code str.isspace}: U+0009-U+000D, U+001C-U+0020, U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F,
     *  U+205F and U+3000. */
    static boolean pySpace(char c) {
        if (c <= ' ') return c == ' ' || c >= '\t' && c <= '\r' || c >= 0x1c && c <= 0x1f;
        if (c < 0x85) return false;
        return c == 0x85 || c == 0xa0 || c == 0x1680 || c >= 0x2000 && c <= 0x200a || c == 0x2028 || c == 0x2029 || c == 0x202f || c == 0x205f || c == 0x3000;
    }
    /** Python's {@code str.strip()}: both ends, every {@link #pySpace} character. */
    static String pyStrip(String text) {
        int start = 0, end = text.length();
        while (start < end && pySpace(text.charAt(start))) start++;
        while (end > start && pySpace(text.charAt(end - 1))) end--;
        return text.substring(start, end);
    }
    /** Python's {@code value.split(None, 1)[1]}: the text after the first word, its leading spaces dropped; null when there is no second part. */
    static String afterFirstWord(String text) {
        int i = 0, length = text.length();
        while (i < length && pySpace(text.charAt(i))) i++;
        while (i < length && !pySpace(text.charAt(i))) i++;
        while (i < length && pySpace(text.charAt(i))) i++;
        return i < length ? text.substring(i) : null;
    }
    /** A line break ends a --name on its line: LF, CR, NEL, LS, PS, FF and VT. */
    static boolean lineBreak(char c) { return c == '\n' || c == '\r' || c == 0x85 || c == 0x2028 || c == 0x2029 || c == 0x0c || c == 0x0b; }
    /** The gap after a --name: any {@link #pySpace} character that is not a line break, NBSP included. */
    static boolean dashesSpace(char c) { return pySpace(c) && !lineBreak(c); }
    static boolean nameChar(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-' || c == '[' || c == ']'; }
    private static boolean nameStart(char c) { return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c == '_'; }
    private static boolean hexDigit(char c) { return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F'; }
    private static boolean paddingFollower(char c) { return c == '=' || c == ' ' || c >= '\t' && c <= '\r' || c == ',' || c == ';' || c == ')' || c == ']' || c == '}' || c == '"' || c == '\''; }
    private static boolean jsonScalarChar(char c) { return c >= '0' && c <= '9' || "+-.eEtrufalsn".indexOf(c) >= 0; }

    /** Whether {@code %3D} or {@code %3d} starts at {@code k}. */
    static boolean encodedEquals(String text, int k) {
        return k + 3 <= text.length() && text.charAt(k) == '%' && text.charAt(k + 1) == '3' && (text.charAt(k + 2) == 'D' || text.charAt(k + 2) == 'd');
    }
    private static boolean encodedSpaceDigits(char a, char b) {
        if (a == '2') return b == '0';
        if (a != '0') return false;
        return b == '9' || b == 'A' || b == 'a' || b == 'B' || b == 'b' || b == 'C' || b == 'c' || b == 'D' || b == 'd';
    }
    private static boolean encodedNbsp(String text, int k) { return k >= 0 && k + 6 <= text.length() && text.regionMatches(true, k, "%C2%A0", 0, 6); }
    private static boolean jsonSpaceEscape(char c) { return c == 't' || c == 'n' || c == 'r' || c == 'f' || c == 'b'; }
    private static int hex4(String text, int k) {
        if (k + 4 > text.length()) return -1;
        int value = 0;
        for (int j = k; j < k + 4; j++) {
            char c = text.charAt(j);
            if (!hexDigit(c)) return -1;
            value = value * 16 + Character.digit(c, 16);
        }
        return value;
    }
    static boolean listHeader(String name) {
        String folded = PrivacyCatalog.normalize(name);
        return folded.equals("cookie") || folded.equals("setcookie") || folded.equals("authorization") || folded.equals("proxyauthorization");
    }

    // ---- Stateless reads (the reference's module functions) ----

    /** Whether another pair starts at {@code start}: [ \t]{0,8} (spaced only), -{0,2}, a name of 1 to 128 characters that starts with a
     *  letter or underscore, [ ]{0,8} (spaced only), then ':' or an '=' that is not padding or an empty value. One forward read. */
    static boolean nextPair(String text, int start, boolean spaced) {
        int length = text.length(), i = start;
        if (spaced) {
            while (i < length && (text.charAt(i) == ' ' || text.charAt(i) == '\t') && i - start <= 8) i++;
            if (i - start > 8) return false;
        }
        int dashes = i;
        while (i < length && text.charAt(i) == '-' && i - dashes <= 2) i++;
        if (i - dashes > 2 || i >= length || !nameStart(text.charAt(i))) return false;
        int name = i++;
        while (i < length && nameChar(text.charAt(i)) && i - name < 128) i++;
        if (i < length && nameChar(text.charAt(i))) return false;
        if (spaced) {
            int gap = i;
            while (i < length && text.charAt(i) == ' ' && i - gap <= 8) i++;
            if (i - gap > 8) return false;
        }
        if (i >= length) return false;
        if (text.charAt(i) == ':') return true;
        if (text.charAt(i) != '=') return false;
        int rest = i + 1;
        if (rest >= length || paddingFollower(text.charAt(rest))) return false;
        // Java's '$' (no MULTILINE) accepts a final line terminator before the end of the text.
        // (CR and LF are padding followers already; NEL, LS and PS are the others.)
        if (rest == length - 1) {
            char tail = text.charAt(rest);
            if (tail == 0x85 || tail == 0x2028 || tail == 0x2029) return false;
        }
        return true;
    }

    /** The true end of a name=value value: a pair boundary with no bracket open, or the end of the text. A value that starts with a
     *  private key header runs to the key's end line; a quoted value is read to its closing quote and then on like any other. */
    static int pairValueEnd(String text, int start) {
        int length = text.length();
        if (start >= length) return length;
        if (text.startsWith("-----BEGIN", start)) {
            int end = text.indexOf("-----END", start);
            if (end < 0) return length;
            int close = text.indexOf("-----", end + 8);
            return close < 0 ? length : close + 5;
        }
        char first = text.charAt(start);
        int i = start;
        if (first == '"' || first == '\'') {
            i = start + 1;
            while (i < length && text.charAt(i) != first) i += text.charAt(i) == '\\' ? 2 : 1;
            if (i >= length) return length;
            i++;
        }
        int depth = 0;
        for (; i < length; i++) {
            char c = text.charAt(i);
            if (c == '(' || c == '[' || c == '{') depth++;
            else if (c == ')' || c == ']' || c == '}') depth = Math.max(depth - 1, 0);
            else if (depth == 0) {
                if ((c == ',' || c == ';' || c == '\n' || c == '\r') && nextPair(text, i + 1, true)) return i;
                if (c == ' ' && nextPair(text, i + 1, false)) return i;
            }
        }
        return length;
    }

    /** The width of the encoded whitespace that ends at {@code end}, read back no further than {@code floor}, or 0: a percent escape
     *  (%20, %09, %0A-%0D or %C2%A0, any hex case) or a JSON string escape (\t \n \r \f \b, or a backslash-u escape of whitespace). A
     *  backslash that an earlier backslash escapes starts no escape. */
    static int encodedSpaceWidth(String text, int end, int floor) {
        if (end - 3 >= floor && text.charAt(end - 3) == '%' && encodedSpaceDigits(text.charAt(end - 2), text.charAt(end - 1))) return 3;
        if (end - 6 >= floor && encodedNbsp(text, end - 6)) return 6;
        int width = 0;
        if (end - 2 >= floor && text.charAt(end - 2) == '\\' && jsonSpaceEscape(text.charAt(end - 1))) width = 2;
        else if (end - 6 >= floor && text.charAt(end - 6) == '\\' && text.charAt(end - 5) == 'u') {
            int code = hex4(text, end - 4);
            if (code >= 0 && pySpace((char)code)) width = 6;
        }
        if (width == 0) return 0;
        int slashes = 1;
        while (end - width - slashes >= floor && text.charAt(end - width - slashes) == '\\') slashes++;
        return slashes % 2 == 1 ? width : 0;
    }

    /** The width of the encoded whitespace that starts at {@code start}, or 0. */
    static int encodedSpaceAhead(String text, int start) {
        int length = text.length();
        if (start >= length) return 0;
        char c = text.charAt(start);
        if (c == '%') {
            if (start + 3 <= length && encodedSpaceDigits(text.charAt(start + 1), text.charAt(start + 2))) return 3;
            return encodedNbsp(text, start) ? 6 : 0;
        }
        if (c == '\\' && start + 1 < length) {
            char next = text.charAt(start + 1);
            if (jsonSpaceEscape(next)) return 2;
            if (next == 'u') {
                int code = hex4(text, start + 2);
                if (code >= 0 && pySpace((char)code)) return 6;
            }
        }
        return 0;
    }

    /** The start of the gap that ends at {@code end}: whitespace ({@link #pySpace}) and encoded whitespace, in any order. */
    static int gapStart(String text, int end, int floor) {
        while (end > floor) {
            if (pySpace(text.charAt(end - 1))) { end--; continue; }
            int width = encodedSpaceWidth(text, end, floor);
            if (width == 0) break;
            end -= width;
        }
        return end;
    }

    /** Whether a percent escape (a '%' and two hex digits) ends at {@code end}. */
    static boolean escapeBefore(String text, int end, int floor) {
        return end - 3 >= floor && text.charAt(end - 3) == '%' && hexDigit(text.charAt(end - 2)) && hexDigit(text.charAt(end - 1));
    }
    private static boolean encodedEqualsBefore(String text, int end) { return text.charAt(end - 2) == '3' && (text.charAt(end - 1) == 'D' || text.charAt(end - 1) == 'd'); }

    /** A name that ends at a percent escape, read back over name characters and %XX escapes (%3D ends it) and percent-decoded once. */
    record Escaped(String decoded, boolean undecided) {}
    static Escaped escapedName(String text, int nameStart, int nameEnd, int floor) {
        boolean endsAtEscape = escapeBefore(text, nameEnd, floor) && !encodedEqualsBefore(text, nameEnd);
        boolean doubleEncoded = nameStart > floor && escapeBefore(text, nameStart, floor) && text.charAt(nameStart - 2) == '2' && text.charAt(nameStart - 1) == '5';
        if (!endsAtEscape && !doubleEncoded) return null;
        int start = nameEnd;
        while (start > floor && nameEnd - start < NAME_CAP) {
            if (escapeBefore(text, start, floor)) {
                if (encodedEqualsBefore(text, start)) break;
                start -= 3;
            } else if (nameChar(text.charAt(start - 1))) start--;
            else break;
        }
        boolean overflow = nameEnd - start >= NAME_CAP && start > floor && (nameChar(text.charAt(start - 1)) || escapeBefore(text, start, floor));
        String decoded = PrivacyCatalog.decode(text.substring(start, nameEnd), false);
        boolean word = false;
        for (int k = 0; k < decoded.length() && !word; k++) word = Character.isLetterOrDigit(decoded.charAt(k)) || decoded.charAt(k) == '_';
        return new Escaped(decoded, overflow || PERCENT_ESCAPE.matcher(decoded).find() || !word);
    }

    /** Whether the encoded whitespace of {@code width} at {@code start} stands for a control character (%09-%0D, \t \n \r \f \b, or a
     *  backslash-u escape of a control); %20, %C2%A0 and an escaped space or separator are encoded spaces, not controls. */
    static boolean encodedControl(String text, int start, int width) {
        if (text.charAt(start) == '%') return width == 3 && !(text.charAt(start + 1) == '2' && text.charAt(start + 2) == '0');
        if (width == 2) return true;
        return Character.getType((char)hex4(text, start + 2)) == Character.CONTROL;
    }

    /** The width of the pair separator at {@code k} ('=', ':' that does not open "://", or %3D / %3d), or 0. */
    static int separatorAt(String text, int k) {
        char c = text.charAt(k);
        if (c == '=' || c == ':' && !text.startsWith("//", k + 1)) return 1;
        return c == '%' && encodedEquals(text, k) ? 3 : 0;
    }

    /** The value end of the pair whose separator (of {@code size}) is at {@code k}: up to 8 spaces after it, then the true end, or the
     *  line end after a list header name. */
    static int separatorValueEnd(String text, int k, int size) {
        int length = text.length(), value = k + size;
        while (value < length && value - k - size < 8 && text.charAt(value) == ' ') value++;
        int name = k;
        while (name > 0 && k - name < NAME_CAP && nameChar(text.charAt(name - 1))) name--;
        if (listHeader(text.substring(name, k))) {
            while (value < length && text.charAt(value) != '\n' && text.charAt(value) != '\r') value++;
            return value;
        }
        return pairValueEnd(text, value);
    }

    interface Decide { int at(int k, int size, int floor); }

    /** No rule leaves an '=', ':' or %3D unread at the end of the text it consumed: while text[start, stop) ends at a separator of a
     *  credential or undecided pair, the replacement runs on to that pair's value end. */
    static int consumedEnd(String text, int start, int stop, Decide decide) {
        while (stop > start) {
            int size;
            char last = text.charAt(stop - 1);
            if ((last == '=' || last == ':') && separatorAt(text, stop - 1) != 0) size = 1;
            else if (stop - 3 >= start && separatorAt(text, stop - 3) == 3) size = 3;
            else return stop;
            int end = decide.at(stop - size, size, start);
            if (end <= stop) return stop;
            stop = end;
        }
        return stop;
    }

    /** Whether the word after a --name and its gap starts with a separator ('=', ':' or %3D). */
    static boolean dashesSeparator(String text, int start) {
        if (start >= text.length()) return false;
        char c = text.charAt(start);
        return c == '=' || c == ':' || c == '%' && encodedEquals(text, start);
    }

    /** The dashes of a flag that starts at {@code i}: 2 for "--", 1 for a single dash and a name start, else 0. */
    static int flagDashes(String text, int i) {
        if (text.startsWith("--", i)) return 2;
        return text.startsWith("-", i) && i + 1 < text.length() && nameStart(text.charAt(i + 1)) ? 1 : 0;
    }

    /** A readable gap: 0 to 8 plain spaces. */
    static boolean readableGap(String text, int start, int end) {
        if (end - start > GAP_CAP) return false;
        for (int k = start; k < end; k++) if (text.charAt(k) != ' ') return false;
        return true;
    }

    /** The quote spelling that ends at {@code end}: &amp;quot;, an escaped quote, a quote or an apostrophe; or null. */
    static String quoteBefore(String text, int end) {
        if (end >= ENTITY_QUOTE.length() && text.startsWith(ENTITY_QUOTE, end - ENTITY_QUOTE.length())) return ENTITY_QUOTE;
        if (end < 1) return null;
        char last = text.charAt(end - 1);
        if (last != '"' && last != '\'') return null;
        if (last == '"') {
            int slashes = 0;
            while (end - 2 - slashes >= 0 && text.charAt(end - 2 - slashes) == '\\' && slashes < NAME_CAP) slashes++;
            if (slashes % 2 == 1) return ESCAPED_QUOTE;
        }
        return String.valueOf(last);
    }

    /** A quoted name that ends, after a gap, before the separator at {@code colon}. opening is -1 when the name is past its window. */
    record JsonName(int opening, String quote, int close, boolean overflow) {}
    static JsonName jsonName(String text, int colon) {
        int end = gapStart(text, colon, 0);
        String quote = quoteBefore(text, end);
        if (quote == null || quote.length() == 1 && end < 3) return null;
        int close = end - quote.length(), floor = Math.max(0, close - QUOTED_NAME_WINDOW);
        // The opening quote is looked for only within the window, never back to the start of the text.
        int opening = -1;
        for (int o = close - quote.length(); o >= floor; o--) if (text.startsWith(quote, o)) { opening = o; break; }
        if (opening >= 0) return new JsonName(opening, quote, close, !readableGap(text, end, colon));
        return floor > 0 ? new JsonName(-1, quote, close, true) : null;
    }

    static int jsonStringEnd(String text, int start) {
        char quote = text.charAt(start);
        int i = start + 1;
        while (i < text.length()) {
            char c = text.charAt(i);
            if (c == '\\') { i += 2; continue; }
            if (c == quote) return i + 1;
            i++;
        }
        return text.length();
    }

    static boolean jsonMemberBoundary(String text, int end) {
        while (end < text.length() && Character.isWhitespace(text.charAt(end))) end++;
        return end >= text.length() || ",}]".indexOf(text.charAt(end)) >= 0;
    }

    static int escapedStringEnd(String text, int start) {
        int length = text.length(), i = start + ESCAPED_QUOTE.length();
        boolean innerEscape = false;
        while (i < length) {
            if (text.charAt(i) == '\\' && i + 1 < length) {
                char outer = text.charAt(i + 1);
                i += 2;
                if (innerEscape) innerEscape = false;
                else if (outer == '\\') innerEscape = true;
                else if (outer == '"') return i;
            } else { i++; innerEscape = false; }
        }
        return length;
    }

    static int entityStringEnd(String text, int start) {
        int length = text.length(), size = ENTITY_QUOTE.length(), i = start + size;
        while (i < length) {
            if (text.charAt(i) == '\\') i += 1 + (text.startsWith(ENTITY_QUOTE, i + 1) ? size : 1);
            else if (text.startsWith(ENTITY_QUOTE, i)) return i + size;
            else i++;
        }
        return length;
    }

    /** The end of the value of a quoted credential name: (a) a string to its closing quote of the same spelling, (b) a number, true,
     *  false or null only before ',', '}', ']' or the end, (c) anything else (objects and arrays included) to its true end. */
    static int jsonValueEnd(String text, int start, boolean listHeader, String quote) {
        int length = text.length();
        if (start >= length) return start;
        if (quote.equals(ENTITY_QUOTE) && text.startsWith(ENTITY_QUOTE, start)) return entityStringEnd(text, start);
        if (quote.equals(ESCAPED_QUOTE) && text.startsWith(ESCAPED_QUOTE, start)) return escapedStringEnd(text, start);
        char first = text.charAt(start);
        if (first == '"' || first == '\'') return jsonStringEnd(text, start);
        int end = start;
        while (end < length && jsonScalarChar(text.charAt(end))) end++;
        if ((end >= length || ",}] \t\r\n".indexOf(text.charAt(end)) >= 0) && jsonMemberBoundary(text, end)) {
            String scalar = text.substring(start, end);
            if (scalar.equals("true") || scalar.equals("false") || scalar.equals("null") || JSON_NUMBER.matcher(scalar).matches()) return end;
        }
        if (listHeader) {
            while (start < length && text.charAt(start) != '\n' && text.charAt(start) != '\r') start++;
            return start;
        }
        return pairValueEnd(text, start);
    }

    /** The index of the last '@' in [from, to), or -1: the search never goes below the start of the token being read. */
    private static int lastAt(String text, int from, int to) {
        for (int at = Math.min(to, text.length()) - 1; at >= from; at--) if (text.charAt(at) == '@') return at;
        return -1;
    }

    // ---- The rule ----

    /**
     * The value of every credential-named pair becomes the placeholder, in one left-to-right pass linear in the text.
     *
     * <p>name=value, name: value and name%3Dvalue: the value runs to its true end ({@link #pairValueEnd}); for the list headers cookie,
     * set-cookie, authorization and proxy-authorization it is the rest of the line. "name": value follows the JSON quoted-name ruling
     * ({@link #jsonValueEnd}) and becomes a quoted placeholder, the name quoted with a quote, an apostrophe, an escaped quote or
     * &amp;quot;; a quoted credential name with '=' or %3D takes its value to the true end. --name value (and a single-dash flag with a
     * full credential name) takes the next word, or after one line break the first word of the next line. The user information of
     * scheme://user:password@host is replaced. A placeholder already in the value is no exception.
     *
     * <p>A name read within a bounded window that runs on past it, a gap between a name and its separator that is not 0 to 8 plain
     * spaces, and a name that ends at a percent escape and still holds one after one decode are undecided: the pair is replaced.
     */
    static String redactCredentialPairs(String text) { return new Pass(text, null, null).run(); }

    /**
     * The rule over a whole URL value: it skips only the spans the catalog itself replaced there ({@code starts}, {@code ends}: sorted,
     * disjoint placeholder spans, recorded by position during the catalog pass; omission spans are not passed and shield nothing).
     */
    static String redactCredentialPairs(String text, int[] starts, int[] ends) { return new Pass(text, starts, ends).run(); }

    /** One run of the rule over one text. */
    private static final class Pass {
        final String text;
        final int length;
        final int[] skipStarts, skipEnds;
        final HashMap<String, Boolean> known = new HashMap<>();
        final Decide decideEnd = (k, size, floor) -> decide(k, size, floor, true);

        Pass(String text, int[] starts, int[] ends) {
            this.text = text; this.length = text.length();
            boolean any = starts != null && starts.length > 0;
            this.skipStarts = any ? starts : null; this.skipEnds = any ? ends : null;
        }

        boolean credential(String name) {
            Boolean found = known.get(name);
            if (found == null) { found = credentialName(name); known.put(name, found); }
            return found;
        }

        /** The end of the catalog span that holds {@code position}, or -1. */
        int skipped(int position) {
            if (skipStarts == null) return -1;
            int lo = 0, hi = skipStarts.length;
            while (lo < hi) { int mid = (lo + hi) >>> 1; if (skipStarts[mid] <= position) lo = mid + 1; else hi = mid; }
            int k = lo - 1;
            return k >= 0 && position < skipEnds[k] ? skipEnds[k] : -1;
        }

        /** The separator branch's decision for the separator at {@code i}: pair (credential or undecided), the name and overflow. */
        record SeparatorPair(boolean pair, String name, boolean overflow) {}
        SeparatorPair separatorPair(int i, int floor) {
            int nameEnd = gapStart(text, i, floor);
            boolean readable = readableGap(text, nameEnd, i);
            int nameStart = nameEnd;
            while (nameStart > floor && nameEnd - nameStart < NAME_CAP && nameChar(text.charAt(nameStart - 1)) && !escapeBefore(text, nameStart, floor)) nameStart--;
            boolean overflow = nameEnd - nameStart >= NAME_CAP && nameStart > floor && nameChar(text.charAt(nameStart - 1)) && !escapeBefore(text, nameStart, floor);
            if (overflow) PrivacyCatalog.path(text.substring(nameStart, nameEnd), false);   // the window alone may be past the depth budget
            Escaped escaped = overflow ? null : escapedName(text, nameStart, nameEnd, floor);
            String plain = text.substring(nameStart, nameEnd), name = escaped == null ? plain : escaped.decoded();
            boolean pair = nameEnd - nameStart >= 2 && (overflow || !readable || credential(plain))
                || escaped != null && (escaped.undecided() || !readable || credential(escaped.decoded()));
            return new SeparatorPair(pair, name, overflow);
        }

        /** Whether another flag starts at {@code start}: "--", or a single-dash flag with a full credential name, then plain whitespace
         *  and more text. */
        boolean flagAhead(int start) {
            int dashes = flagDashes(text, start);
            if (dashes != 1) return dashes == 2;
            int end = start + 1;
            while (end < length && nameChar(text.charAt(end)) && end - start <= NAME_CAP) end++;
            if (end - start < 3 || end - start > NAME_CAP || end >= length || !pySpace(text.charAt(end))) return false;
            int after = end;
            while (after < length && pySpace(text.charAt(after))) after++;
            return after < length && credential(text.substring(start, end));
        }

        /** Whether the line read fresh at {@code start} begins with a pair, by any rule, or another flag. */
        boolean lineStartsPair(int start) {
            int k = -1;
            for (String quote : List.of(ENTITY_QUOTE, ESCAPED_QUOTE, "\"", "'")) {
                if (text.startsWith(quote, start)) {
                    int from = start + quote.length(), limit = Math.min(length, from + QUOTED_NAME_WINDOW);
                    int close = -1;
                    for (int o = from; o + quote.length() <= limit; o++) if (text.startsWith(quote, o)) { close = o; break; }
                    if (close < 0) return false;
                    k = close + quote.length();
                    break;
                }
            }
            if (k < 0) {
                if (flagAhead(start)) return true;
                int name = start;
                while (name < length && text.charAt(name) == '-' && name - start < 2) name++;
                k = name;
                while (k < length && k - name < NAME_CAP) {
                    if (nameChar(text.charAt(k))) k++;
                    else if (k + 3 <= length && escapeBefore(text, k + 3, k) && !encodedEqualsBefore(text, k + 3) && encodedSpaceAhead(text, k) == 0) k += 3;
                    else break;
                }
                if (k == name) return false;
            }
            while (k < length && (dashesSpace(text.charAt(k)) || encodedSpaceAhead(text, k) != 0)) { int w = encodedSpaceAhead(text, k); k += w != 0 ? w : 1; }
            return k < length && separatorAt(text, k) != 0;
        }

        /** Whether the quoted name is a credential or undecided pair; and the name. */
        record QuotedPair(boolean pair, String name) {}
        QuotedPair quotedPair(JsonName quoted) {
            String name = quoted.opening() < 0 ? "" : text.substring(quoted.opening() + quoted.quote().length(), quoted.close());
            int plain = name.length();
            while (plain > 0 && nameChar(name.charAt(plain - 1)) && !escapeBefore(name, plain, 0)) plain--;
            Escaped escaped = quoted.opening() >= 0 ? escapedName(name, plain, name.length(), 0) : null;
            boolean pair = quoted.overflow() || credential(name) || escaped != null && (escaped.undecided() || credential(escaped.decoded()));
            return new QuotedPair(pair, name);
        }

        /** The value end of a quoted name with '=' or %3D at {@code k}: up to 8 spaces, then the true end, or the line end for a list header. */
        int equalsValueEnd(int k, int size, String name) {
            int value = k + size;
            while (value < length && value - k - size < 8 && text.charAt(value) == ' ') value++;
            if (listHeader(name)) {
                while (value < length && text.charAt(value) != '\n' && text.charAt(value) != '\r') value++;
                return value;
            }
            return pairValueEnd(text, value);
        }

        /** A separator inside or at the end of consumed text, handed to the branch that reads it at {@code k}: the end of that pair's
         *  value when it is a credential or undecided pair ({@code k} itself when {@code readEnd} is false), else -1. */
        int decide(int k, int size, int floor, boolean readEnd) {
            JsonName quoted = jsonName(text, k);
            if (quoted != null && quoted.opening() >= floor) {
                QuotedPair qp = quotedPair(quoted);
                if (size == 1 && text.charAt(k) == ':') {
                    if (!qp.pair()) return -1;
                    if (!readEnd) return k;
                    int value = k + 1;
                    while (value < length && Character.isWhitespace(text.charAt(value))) value++;
                    return jsonValueEnd(text, value, listHeader(qp.name()), quoted.quote());
                }
                if (qp.pair()) return readEnd ? equalsValueEnd(k, size, qp.name()) : k;
            }
            if (!separatorPair(k, floor).pair()) return -1;
            return readEnd ? separatorValueEnd(text, k, size) : k;
        }

        boolean quotedEquals(int k) {
            JsonName quoted = k > 0 ? jsonName(text, k) : null;
            return quoted != null && quotedPair(quoted).pair();
        }

        /** The end of a --name's value word that starts at {@code value}; the replacement starts at {@code begin}. */
        int wordEnd(int begin, int value) {
            int valueEnd = value, word = value;
            char q = text.charAt(value);
            if (q == '"' || q == '\'') {
                valueEnd = value + 1;
                while (valueEnd < length && text.charAt(valueEnd) != q) valueEnd += text.charAt(valueEnd) == '\\' ? 2 : 1;
                valueEnd = word = Math.min(length, valueEnd + 1);
            }
            int shapeEnd = PrivacyCatalog.INSTANCE.wordShapeEnd(text, value);
            int first = -1, last = -1;
            while (valueEnd < length && (valueEnd < shapeEnd || !pySpace(text.charAt(valueEnd)))) {
                int size = valueEnd >= word ? separatorAt(text, valueEnd) : 0;
                if (size != 0 && decide(valueEnd, size, begin, false) >= 0) {
                    if (first < 0) first = valueEnd;
                    last = valueEnd;
                }
                valueEnd++;
            }
            int ahead = valueEnd;
            while (ahead < length && (dashesSpace(text.charAt(ahead)) || encodedSpaceAhead(text, ahead) != 0)) { int w = encodedSpaceAhead(text, ahead); ahead += w != 0 ? w : 1; }
            if (ahead > valueEnd && ahead < length && separatorAt(text, ahead) != 0 && decide(ahead, separatorAt(text, ahead), begin, false) >= 0) {
                if (first < 0) first = ahead;
                last = ahead;
            }
            if (first >= 0) valueEnd = Math.max(valueEnd, decide(first, separatorAt(text, first), begin, true));
            if (last >= 0 && last != first) valueEnd = Math.max(valueEnd, decide(last, separatorAt(text, last), begin, true));
            return consumedEnd(text, begin, valueEnd, decideEnd);
        }

        String run() {
            StringBuilder out = null;
            int copied = 0, i = 0, dashesColon = -1;
            while (i < length) {
                char c = text.charAt(i);
                int start = -1, stop = -1;
                String replacement = REDACTED;
                boolean userinfo = false;
                JsonName quoted = c == ':' && i > 0 ? jsonName(text, i) : null;
                if (quoted != null) {
                    QuotedPair qp = quotedPair(quoted);
                    if (qp.pair()) {
                        int value = i + 1;
                        while (value < length && Character.isWhitespace(text.charAt(value))) value++;
                        if (skipStarts != null && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
                        boolean header = listHeader(qp.name());
                        int end = jsonValueEnd(text, value, header, quoted.quote());
                        if (!header) end = consumedEnd(text, value, end, decideEnd);
                        if (end > value) { start = value; stop = end; replacement = quoted.quote() + REDACTED + quoted.quote(); }
                    }
                    if (start < 0) { i++; continue; }
                } else if (c == ':' && text.startsWith("//", i + 1) && i != dashesColon) {
                    int limit = Math.min(length, i + 3 + 512), end = i + 3;
                    while (end < limit && "/?# \t\r\n".indexOf(text.charAt(end)) < 0) end++;
                    int at = lastAt(text, i + 3, end);
                    if (at < 0) {
                        // A password may hold '/', '?' or '#': a user:password form before a later '@' in the same word runs to it.
                        int word = i + 3;
                        while (word < length && word - i < 1024 && !Character.isWhitespace(text.charAt(word))) word++;
                        int later = lastAt(text, i + 3, word);
                        int colon = -1;
                        for (int k = i + 3; k < later && colon < 0; k++) if (text.charAt(k) == ':') colon = k;
                        if (later > i + 3 && colon > i + 3) at = later;
                    }
                    if (at < 0) { i += 3; continue; }
                    start = i + 3; stop = at; userinfo = true;
                } else if ((c == '=' || c == '%' && encodedEquals(text, i)) && quotedEquals(i)) {
                    // A quoted name with '=' or %3D that is a credential or undecided pair: the value runs to its true end.
                    int size = c == '=' ? 1 : 3, value = i + size;
                    while (value < length && value - i - size < 8 && text.charAt(value) == ' ') value++;
                    if (skipStarts != null && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
                    String name = quotedPair(jsonName(text, i)).name();
                    start = value;
                    stop = equalsValueEnd(i, size, name);
                    if (!listHeader(name)) stop = consumedEnd(text, value, stop, decideEnd);
                } else if (c == '=' || c == ':' || c == '%' && encodedEquals(text, i)) {
                    // The separator: '=', ':', or '=' percent-encoded. The gap before it is read back whole.
                    int size = c == '%' ? 3 : 1;
                    SeparatorPair sp = separatorPair(i, copied);
                    if (sp.pair()) {
                        int value = i + size;
                        while (value < length && value - i - size < 8 && text.charAt(value) == ' ') value++;
                        if (skipStarts != null && skipped(value) >= 0) { i = Math.max(skipped(value), i + 1); continue; }
                        start = value;
                        if (!sp.overflow() && listHeader(sp.name())) {
                            stop = value;
                            while (stop < length && text.charAt(stop) != '\n' && text.charAt(stop) != '\r') stop++;
                        } else stop = consumedEnd(text, value, pairValueEnd(text, value), decideEnd);
                    }
                } else if (c == '-' && (i == 0 || pySpace(text.charAt(i - 1)) || encodedSpaceWidth(text, i, 0) != 0) && flagDashes(text, i) != 0) {
                    // A --name, or a single-dash flag with a full credential name (never -p or a cluster that is no credential name).
                    int dashes = flagDashes(text, i), end = i + dashes;
                    while (end < length && nameChar(text.charAt(end)) && end - i < NAME_CAP + dashes) end++;
                    boolean overflow = end - i >= NAME_CAP + dashes && end < length && nameChar(text.charAt(end));
                    while (overflow && end < length && nameChar(text.charAt(end))) end++;
                    if (overflow) PrivacyCatalog.path(text.substring(i, end), false);
                    if (end < length && end - i - dashes >= (dashes == 2 ? 1 : 2)
                            && (dashesSpace(text.charAt(end)) || lineBreak(text.charAt(end)) || encodedSpaceAhead(text, end) != 0)
                            && (overflow || credential(text.substring(i, end)))) {
                        int value = end, control = -1, gapEnd = end;
                        boolean spaced = false;
                        while (value < length) {
                            if (dashesSpace(text.charAt(value))) { value++; spaced = spaced || control >= 0; continue; }
                            int width = encodedSpaceAhead(text, value);
                            if (width == 0) break;
                            if (control < 0 && encodedControl(text, value, width)) control = value;
                            value += width;
                            gapEnd = value;
                        }
                        if (dashesSeparator(text, value)) {
                            // A separator after the gap: the pair is name<gap>=value, read at the separator (its gap rule included).
                            dashesColon = text.charAt(value) == ':' ? value : -1;
                            i = value;
                            continue;
                        }
                        int begin = control >= 0 ? control : value, valueEnd = -1;
                        if (value < length && lineBreak(text.charAt(value))) {
                            // After ONE line break the next line is read fresh; a blank line ends the --name.
                            int line = value + (text.startsWith("\r\n", value) ? 2 : 1);
                            boolean encoded = false;
                            while (line < length) {
                                if (dashesSpace(text.charAt(line))) { line++; continue; }
                                if (encoded && lineBreak(text.charAt(line))) { line += text.startsWith("\r\n", line) ? 2 : 1; encoded = false; continue; }
                                int width = encodedSpaceAhead(text, line);
                                if (width == 0) break;
                                if (control < 0 && encodedControl(text, line, width)) control = line;
                                line += width;
                                encoded = true;
                            }
                            if (line >= length || lineBreak(text.charAt(line)) || dashesSeparator(text, line)) {
                                if (line >= length || lineBreak(text.charAt(line))) i = end;
                                else { dashesColon = text.charAt(line) == ':' ? line : -1; i = line; }
                                continue;
                            }
                            if (lineStartsPair(line)) {
                                if (control >= 0) { start = control; stop = gapEnd; }
                                else { i = line; continue; }
                            } else {
                                begin = control >= 0 ? control : line;
                                valueEnd = wordEnd(begin, line);
                            }
                        } else if (control >= 0 && !spaced && value < length) {
                            valueEnd = wordEnd(begin, value);
                        } else if (value >= length || flagAhead(value)) {
                            if (control >= 0) { start = control; stop = gapEnd; }
                        } else {
                            valueEnd = wordEnd(begin, value);
                        }
                        if (valueEnd > begin) { start = begin; stop = valueEnd; }
                    }
                    if (start < 0) { i = end; continue; }
                }
                if (start >= 0 && skipStarts != null) {
                    int spanEnd = skipped(start);
                    // User information that runs on past the catalog's own (to a later '@') is replaced whole, the span with it.
                    if (spanEnd >= 0 && !(userinfo && stop > spanEnd)) { i = Math.max(spanEnd, i + 1); continue; }
                }
                if (start >= copied && stop >= start) {
                    if (out == null) out = new StringBuilder(length);
                    out.append(text, copied, start).append(replacement);
                    copied = stop;
                    i = Math.max(stop, i + 1);
                    continue;
                }
                i++;
            }
            return out == null ? text : out.append(text, copied, length).toString();
        }
    }
}
