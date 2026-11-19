package ai.traceai.cohere;

import java.util.ArrayList;
import java.util.List;

/** Finds raw-output writes in Java source text: every write must sit inside the one shared helper. */
final class RawOutputScanner {
    static final String HELPER = "setRawOutputUnlessHidden";
    static final List<String> MARKERS = List.of("setRawOutput(", "RAW_OUTPUT", "fi.raw_output", "raw.output");

    record Result(List<String> outside, int inside) {}

    private RawOutputScanner() {}

    static Result scan(String name, String source) {
        List<String> lines = List.of(source.split("\\R", -1));
        List<String> outside = new ArrayList<>();
        int inside = 0;
        int helperStart = -1, helperEnd = -1, depth = 0;
        for (int i = 0; i < lines.size(); i++) {
            String line = lines.get(i);
            if (helperStart < 0 && line.contains("void " + HELPER + "(")) helperStart = i;
            if (helperStart >= 0 && helperEnd < 0) {
                for (char c : line.toCharArray()) {
                    if (c == '{') depth++;
                    if (c == '}') { depth--; if (depth == 0) { helperEnd = i; break; } }
                }
            }
        }
        for (int i = 0; i < lines.size(); i++) {
            String code = lines.get(i).trim();
            if (code.startsWith("//") || code.startsWith("*") || code.startsWith("/*")) continue;
            if (MARKERS.stream().noneMatch(code::contains)) continue;
            if (helperStart >= 0 && i > helperStart && i <= helperEnd) inside++;
            else outside.add(name + ":" + (i + 1) + ": " + code);
        }
        return new Result(outside, inside);
    }
}
