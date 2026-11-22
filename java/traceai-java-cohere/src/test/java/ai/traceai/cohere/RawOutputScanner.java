package ai.traceai.cohere;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Finds raw-output writes in Java source text. Every write must sit inside the one shared helper, and every method
 * of that name must itself test all four hide settings before it writes.
 */
final class RawOutputScanner {
    static final String HELPER = "setRawOutputUnlessHidden";
    /** A call (any spacing), a method reference, the attribute constant and the attribute key strings. */
    private static final Pattern WRITE = Pattern.compile(
        "\\bsetRawOutput\\s*\\(|::\\s*setRawOutput\\b|\\bRAW_OUTPUT\\b|fi\\.raw_output|raw\\.output");
    private static final Pattern HELPER_DECLARATION = Pattern.compile("\\b" + HELPER + "\\s*\\([^;{)]*\\)\\s*(?:throws[^;{]*)?\\{");
    private static final List<String> FLAGS = List.of("isHideInputs", "isHideInputMessages", "isHideOutputs", "isHideOutputMessages");

    record Result(List<String> outside, int inside) {}

    private RawOutputScanner() {}

    static Result scan(String name, String source) {
        String code = withoutComments(source);
        List<String> outside = new ArrayList<>();
        // The helper bodies: [open brace, matching close brace] of every method named like the helper.
        List<int[]> helpers = new ArrayList<>();
        Matcher declaration = HELPER_DECLARATION.matcher(code);
        while (declaration.find()) {
            int open = declaration.end() - 1;
            int close = matchingBrace(code, open);
            helpers.add(new int[] {open, close});
            String body = code.substring(open, close < 0 ? code.length() : close);
            for (String flag : FLAGS) {
                if (!body.contains(flag)) {
                    outside.add(name + ":" + lineOf(code, open) + ": the " + HELPER + " method does not test " + flag);
                }
            }
        }
        if (helpers.size() > 1) outside.add(name + ": more than one method named " + HELPER);
        int inside = 0;
        Matcher write = WRITE.matcher(code);
        while (write.find()) {
            int at = write.start();
            boolean inHelper = helpers.stream().anyMatch(h -> at > h[0] && (h[1] < 0 || at < h[1]));
            if (inHelper) inside++;
            else outside.add(name + ":" + lineOf(code, at) + ": " + lineText(code, at));
        }
        return new Result(outside, inside);
    }

    /** The source with every comment replaced by spaces (newlines kept, string and char literals respected). */
    static String withoutComments(String source) {
        StringBuilder out = new StringBuilder(source.length());
        int i = 0, n = source.length();
        while (i < n) {
            char c = source.charAt(i);
            char next = i + 1 < n ? source.charAt(i + 1) : '\0';
            if (c == '/' && next == '/') {
                while (i < n && source.charAt(i) != '\n') { out.append(' '); i++; }
            } else if (c == '/' && next == '*') {
                out.append("  "); i += 2;
                while (i < n && !(source.charAt(i) == '*' && i + 1 < n && source.charAt(i + 1) == '/')) {
                    out.append(source.charAt(i) == '\n' ? '\n' : ' '); i++;
                }
                if (i < n) { out.append("  "); i += 2; }
            } else if (c == '"' && source.startsWith("\"\"\"", i)) {
                int end = source.indexOf("\"\"\"", i + 3);
                int stop = end < 0 ? n : end + 3;
                out.append(source, i, stop); i = stop;
            } else if (c == '"' || c == '\'') {
                out.append(c); i++;
                while (i < n && source.charAt(i) != c && source.charAt(i) != '\n') {
                    if (source.charAt(i) == '\\' && i + 1 < n) { out.append(source.charAt(i)); i++; }
                    out.append(source.charAt(i)); i++;
                }
                if (i < n && source.charAt(i) == c) { out.append(c); i++; }
            } else {
                out.append(c); i++;
            }
        }
        return out.toString();
    }

    /** Index of the brace closing the one at {@code open}, ignoring braces in string and char literals; -1 if none. */
    private static int matchingBrace(String code, int open) {
        int depth = 0;
        for (int i = open; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '"' || c == '\'') {
                for (i++; i < code.length() && code.charAt(i) != c && code.charAt(i) != '\n'; i++) {
                    if (code.charAt(i) == '\\') i++;
                }
            } else if (c == '{') {
                depth++;
            } else if (c == '}' && --depth == 0) {
                return i;
            }
        }
        return -1;
    }

    private static int lineOf(String code, int index) {
        int line = 1;
        for (int i = 0; i < index; i++) if (code.charAt(i) == '\n') line++;
        return line;
    }

    private static String lineText(String code, int index) {
        int start = code.lastIndexOf('\n', index) + 1;
        int end = code.indexOf('\n', index);
        return code.substring(start, end < 0 ? code.length() : end).trim();
    }
}
