package ai.traceai.ollama;

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
    @Test
    void everyRawOutputWriteIsInsideTheSharedHelper() throws IOException {
        List<String> outside = new ArrayList<>();
        int inside = 0;
        try (Stream<Path> files = Files.walk(Path.of("src", "main", "java"))) {
            for (Path file : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                RawOutputScanner.Result result = RawOutputScanner.scan(file.toString(), Files.readString(file));
                outside.addAll(result.outside());
                inside += result.inside();
            }
        }
        assertThat(outside).as("raw-output writes outside the %s helper", RawOutputScanner.HELPER).isEmpty();
        assertThat(inside).as("the helper writes the raw output").isPositive();
    }
}
