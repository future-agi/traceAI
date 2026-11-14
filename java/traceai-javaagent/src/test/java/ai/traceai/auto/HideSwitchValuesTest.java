package ai.traceai.auto;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import java.nio.file.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

/**
 * A hide switch someone set must never silently do nothing. For every FI_HIDE_* variable, its init option and its
 * futureagi.toml setting: true, 1, yes and on hide; false, 0, no and off show; a blank value is not set; any other
 * value hides. A hide line in the file is never dropped, however its value is written. The warning is printed once
 * per variable in a JVM, so these cases check only what is hidden; the warning, and its absence for a single-quoted
 * value, are checked in a child JVM by AgentE2ETest. Other switches keep their own parsing. A config file that is
 * there but cannot be used is reported once, by its path and never by its content.
 */
class HideSwitchValuesTest {
    private static final String NO_FILE = "target/no-hide-switch-config.toml";
    /** One attribute that each switch hides when it is the only switch set. */
    private static final Map<String, String> SWITCHES = Map.of(
        "FI_HIDE_INPUTS", "input.value", "FI_HIDE_INPUT_MESSAGES", "gen_ai.input.messages.0.message.content", "FI_HIDE_INPUT_TEXT", "input.value",
        "FI_HIDE_OUTPUTS", "output.value", "FI_HIDE_OUTPUT_MESSAGES", "gen_ai.output.messages.0.message.content", "FI_HIDE_OUTPUT_TEXT", "output.value",
        "FI_HIDE_INPUT_IMAGES", "gen_ai.image.url", "FI_HIDE_LLM_INVOCATION_PARAMETERS", "llm.invocation_parameters");
    private static AutoConfig env(String name, String value) { return AutoConfig.resolve(Map.of("config_file", NO_FILE), Map.of(name, value)); }
    private static boolean hides(AutoConfig config, String name) { return TraceAIAuto.hidden(SWITCHES.get(name), null, config); }

    @ParameterizedTest @ValueSource(strings = {"true", "on", "yes", "1", " TRUE ", "On"})
    void trueValuesHide(String value) {
        AutoConfig config = env("FI_HIDE_INPUTS", value);
        assertTrue(config.masking.isHideInputs(), value); assertTrue(hides(config, "FI_HIDE_INPUTS"), value); assertTrue(TraceAIAuto.hidesContent(config), value);
    }
    @ParameterizedTest @ValueSource(strings = {"false", "off", "no", "0", " OFF "})
    void falseValuesShow(String value) {
        AutoConfig config = env("FI_HIDE_INPUTS", value);
        assertFalse(config.masking.isHideInputs(), value); assertFalse(hides(config, "FI_HIDE_INPUTS"), value); assertFalse(TraceAIAuto.hidesContent(config), value);
    }
    @ParameterizedTest @ValueSource(strings = {"", "   "})
    void blankValueIsNotSet(String value) throws Exception {
        assertFalse(env("FI_HIDE_INPUTS", value).masking.isHideInputs());
        // A blank environment value or init option does not switch off what a lower source hides.
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-", ".toml");
        try {
            Files.writeString(file, "hide_inputs = true\n");
            assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of("FI_HIDE_INPUTS", value)).masking.isHideInputs(), "blank environment value over the file");
            assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString(), "hide_inputs", value), Map.of()).masking.isHideInputs(), "blank init option over the file");
            assertTrue(AutoConfig.resolve(Map.of("config_file", NO_FILE, "hide_inputs", value), Map.of("FI_HIDE_INPUTS", "true")).masking.isHideInputs(), "blank init option over the environment");
        } finally { Files.deleteIfExists(file); }
    }
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS"})
    void everySwitchReadsOnAndOffAndHidesOnAnUnknownValue(String name) {
        assertFalse(hides(AutoConfig.resolve(Map.of("config_file", NO_FILE), Map.of()), name), name + " not set");
        assertTrue(hides(env(name, "on"), name), name + "=on");
        assertFalse(hides(env(name, "off"), name), name + "=off");
        assertTrue(hides(env(name, "maybe"), name), name + "=maybe");
        assertTrue(hides(env(name, "enabled"), name), name + "=enabled");
        // The init option follows the same rule.
        String option = name.substring(3).toLowerCase(Locale.ROOT);
        assertTrue(hides(AutoConfig.resolve(Map.of("config_file", NO_FILE, option, "on"), Map.of()), name), option + "=on");
        assertTrue(hides(AutoConfig.resolve(Map.of("config_file", NO_FILE, option, "maybe"), Map.of()), name), option + "=maybe");
    }
    @Test void configFileValuesFollowTheSameRule() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-", ".toml");
        try {
            for (String value : List.of("true", "\"on\"", "on", "\" Yes \"", "1", "\"maybe\"", "maybe")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
            for (String value : List.of("false", "\"off\"", "off", "0", "\"\"")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertFalse(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
            // A single-quoted string is read as its text.
            for (String value : List.of("'on'", "' Yes '", "'maybe'", "'on' # hide prompts")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
            for (String value : List.of("'off'", "' No '", "''")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertFalse(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
            // A hide line is never dropped: a value the file reader cannot decode is kept as written, and so hides.
            for (String value : List.of("\"on", "\"\"\"on\"\"\"", "[on]", "[\"on\"")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
        } finally { Files.deleteIfExists(file); }
    }
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS"})
    void everySwitchReadsItsConfigFileSettingHoweverTheValueIsWritten(String name) throws Exception {
        String option = name.substring(3).toLowerCase(Locale.ROOT);
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-", ".toml");
        try {
            for (String value : List.of("'on'", "\"on\"", "yes", "\"on")) {
                Files.writeString(file, option + " = " + value + "\n");
                assertTrue(hides(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()), name), option + " = " + value);
            }
            for (String value : List.of("'off'", "\"off\"", "no")) {
                Files.writeString(file, option + " = " + value + "\n");
                assertFalse(hides(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()), name), option + " = " + value);
            }
        } finally { Files.deleteIfExists(file); }
    }
    /**
     * Resolves the configuration twice with the given config file, which sets hide_inputs when it can be read, and
     * returns what was printed to standard error. The file's settings must not apply either time.
     */
    private static String errorsWhenResolvedTwice(String configFile) {
        var output = new java.io.ByteArrayOutputStream(); var stderr = System.err;
        try (var captured = new java.io.PrintStream(output, true, java.nio.charset.StandardCharsets.UTF_8)) {
            System.setErr(captured);
            for (int i = 0; i < 2; i++) assertFalse(AutoConfig.resolve(Map.of("config_file", configFile), Map.of()).masking.isHideInputs(), "the file's settings do not apply");
        } finally { System.setErr(stderr); }
        return output.toString(java.nio.charset.StandardCharsets.UTF_8);
    }
    /**
     * A config file that is there but cannot be used must say so: the hide settings in it do nothing, and that must
     * never be silent. One warning per JVM names the file's path and what failed, never the file's content.
     */
    @Test void configFileThatIsNotValidUtf8IsReportedOnceByPathAndNeverByContent() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-not-utf8-", ".toml");
        try {
            // Saved as Latin-1: the last letter of the comment is one byte that is not valid UTF-8.
            Files.write(file, "hide_inputs = true # PRIVATE_NOTE café\n".getBytes(java.nio.charset.StandardCharsets.ISO_8859_1));
            String output = errorsWhenResolvedTwice(file.toString());
            List<String> warnings = output.lines().filter(line -> line.contains(file.toString())).toList();
            assertEquals(1, warnings.size(), output);
            assertTrue(warnings.get(0).contains("not valid UTF-8") && warnings.get(0).contains("none of its settings apply"), output);
            assertFalse(output.contains("PRIVATE_NOTE"), output);
        } finally { Files.deleteIfExists(file); }
    }
    @Test void configFileThatCannotBeReadIsReportedOnceByPathAndNeverByContent() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-unreadable-", ".toml");
        try {
            Files.writeString(file, "hide_inputs = true # PRIVATE_NOTE\n");
            // Taking away the permission to read does not stop every user (root reads any file); the case is skipped then.
            org.junit.jupiter.api.Assumptions.assumeTrue(file.toFile().setReadable(false) && !Files.isReadable(file), "this user can read the file whatever its permissions");
            String output = errorsWhenResolvedTwice(file.toString());
            List<String> warnings = output.lines().filter(line -> line.contains(file.toString())).toList();
            assertEquals(1, warnings.size(), output);
            assertTrue(warnings.get(0).contains("could not be read") && warnings.get(0).contains("none of its settings apply"), output);
            assertFalse(output.contains("PRIVATE_NOTE"), output);
        } finally { file.toFile().setReadable(true); Files.deleteIfExists(file); }
    }
    @Test void configFileThatIsNotThereIsNotReported() {
        assertFalse(Files.exists(Path.of(NO_FILE)));
        String output = errorsWhenResolvedTwice(NO_FILE);
        assertTrue(output.lines().noneMatch(line -> line.contains(NO_FILE)), output);
    }
    @Test void otherSwitchesKeepTheirOwnParsing() {
        for (String value : List.of("on", "yes", "1", " TRUE ", "maybe")) {
            AutoConfig config = AutoConfig.resolve(Map.of("config_file", NO_FILE), Map.of("FI_WIRE_CAPTURE", value, "FI_AUTO_DEBUG", value, "OTEL_SDK_DISABLED", value));
            assertFalse(config.flag("FI_WIRE_CAPTURE"), value); assertFalse(config.flag("FI_AUTO_DEBUG"), value); assertFalse(config.flag("OTEL_SDK_DISABLED"), value);
            assertFalse(TraceAIAuto.hidesContent(config), value);
        }
        AutoConfig config = AutoConfig.resolve(Map.of("config_file", NO_FILE), Map.of("FI_WIRE_CAPTURE", "TRUE", "FI_AUTO_DEBUG", "True", "OTEL_SDK_DISABLED", "true"));
        assertTrue(config.flag("FI_WIRE_CAPTURE")); assertTrue(config.flag("FI_AUTO_DEBUG")); assertTrue(config.flag("OTEL_SDK_DISABLED"));
    }
}
