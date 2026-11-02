package ai.traceai;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/** Fork instead of mutating System.getenv, which is JVM-global and platform dependent. */
class TraceConfigEnvironmentTest {
    private static final List<String> FLAGS = List.of("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_ERROR_DETAILS");

    @ParameterizedTest
    @ValueSource(strings = {"unset", "true", "TrUe", "false", "invalid"})
    void environmentDefaultsAndExplicitOverrides(String value) throws Exception {
        Map<String, String> environment = new LinkedHashMap<>();
        if (!value.equals("unset")) FLAGS.forEach(flag -> environment.put(flag, value));
        runProbe(environment, Boolean.parseBoolean(value) ? 31 : 0);
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 2, 4, 8, 16})
    void eachEnvironmentFlagIsIndependent(int flags) throws Exception {
        Map<String, String> environment = new LinkedHashMap<>();
        for (int i = 0; i < FLAGS.size(); i++) {
            environment.put(FLAGS.get(i), Boolean.toString((flags & (1 << i)) != 0));
        }
        runProbe(environment, flags);
    }

    private void runProbe(Map<String, String> environment, int expected) throws Exception {
        ProcessBuilder builder = new ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-cp", System.getProperty("java.class.path"), Probe.class.getName(), Integer.toString(expected));
        FLAGS.forEach(builder.environment()::remove);
        builder.environment().putAll(environment);
        Process process = builder.redirectErrorStream(true).start();
        try {
            assertThat(process.waitFor(30, TimeUnit.SECONDS)).isTrue();
            assertThat(process.exitValue()).withFailMessage(new String(process.getInputStream().readAllBytes())).isZero();
        } finally {
            process.destroyForcibly();
        }
    }

    public static class Probe {
        public static void main(String[] args) {
            int expected = Integer.parseInt(args[0]);
            check(TraceConfig.builder().build(), expected);
            check(TraceConfig.fromEnvironment(), expected);
            check(TraceConfig.builder().hideInputs(false).build(), expected & ~1);
            check(TraceConfig.builder().hideOutputs(false).build(), expected & ~2);
            check(TraceConfig.builder().hideInputMessages(false).build(), expected & ~4);
            check(TraceConfig.builder().hideOutputMessages(false).build(), expected & ~8);
            for (boolean override : List.of(false, true)) {
                check(TraceConfig.builder().hideInputs(override).hideOutputs(override)
                    .hideInputMessages(override).hideOutputMessages(override)
                    .hideErrorDetails(override).build(), override ? 31 : 0);
            }
        }

        private static void check(TraceConfig config, int expected) {
            assertThat(config.isHideInputs()).as("hideInputs").isEqualTo((expected & 1) != 0);
            assertThat(config.isHideOutputs()).as("hideOutputs").isEqualTo((expected & 2) != 0);
            assertThat(config.isHideInputMessages()).as("hideInputMessages").isEqualTo((expected & 4) != 0);
            assertThat(config.isHideOutputMessages()).as("hideOutputMessages").isEqualTo((expected & 8) != 0);
            assertThat(config.isHideErrorDetails()).as("hideErrorDetails").isEqualTo((expected & 16) != 0);
        }
    }
}
