package ai.traceai.cohere;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Every raw-output write in this module's main sources must sit inside the one shared helper, so a response
 * that echoes request content cannot be recorded under a hide setting by a call site added later.
 */
class RawOutputWritesCensusTest {
    static final String HELPER = "setRawOutputUnlessHidden";
    static final List<String> MARKERS = List.of("setRawOutput(", "RAW_OUTPUT", "fi.raw_output", "raw.output");

    @Test
    void everyRawOutputWriteIsInsideTheSharedHelper() throws IOException {
        List<String> outside = new ArrayList<>();
        int inside = 0;
        try (Stream<Path> files = Files.walk(Path.of("src", "main", "java"))) {
            for (Path file : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                List<String> lines = Files.readAllLines(file);
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
                    else outside.add(file + ":" + (i + 1) + ": " + code);
                }
            }
        }
        assertThat(outside).as("raw-output writes outside the %s helper", HELPER).isEmpty();
        assertThat(inside).as("the helper writes the raw output").isPositive();
    }
}
