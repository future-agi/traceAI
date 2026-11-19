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
        "FI_HIDE_INPUT_IMAGES", "gen_ai.image.url", "FI_HIDE_LLM_INVOCATION_PARAMETERS", "llm.invocation_parameters", "FI_HIDE_ERROR_DETAILS", "exception.message");
    private static final byte[] BYTE_ORDER_MARK = {(byte)0xEF, (byte)0xBB, (byte)0xBF};
    private static final List<String> ERROR_KEYS = List.of("exception.message", "exception.stacktrace", "error.message", "error.stack");
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
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS", "FI_HIDE_ERROR_DETAILS"})
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
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS", "FI_HIDE_ERROR_DETAILS"})
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
    /** The same kind of line in a file written with a UTF-8 byte order mark first, as Windows editors write it. */
    @Test void configFileThatStartsWithAByteOrderMarkIsReadFromItsFirstLine() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-bom-", ".toml");
        try {
            byte[] text = "hide_inputs = true\nhide_outputs = false\n".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            byte[] bytes = new byte[BYTE_ORDER_MARK.length + text.length];
            System.arraycopy(BYTE_ORDER_MARK, 0, bytes, 0, BYTE_ORDER_MARK.length); System.arraycopy(text, 0, bytes, BYTE_ORDER_MARK.length, text.length);
            Files.write(file, bytes);
            AutoConfig config = AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of());
            assertTrue(config.masking.isHideInputs(), "the first line is read although the file starts with a byte order mark");
            assertFalse(config.masking.isHideOutputs());
            assertTrue(hides(config, "FI_HIDE_INPUTS"));
        } finally { Files.deleteIfExists(file); }
    }
    /** A valid array on a hide key is read as its items. */
    @Test void configFileArrayOnAHideKeyIsReadAsItsItems() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-array-", ".toml");
        try {
            for (String value : List.of("[\"on\"]", "[ \"yes\" ]")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
            for (String value : List.of("[\"off\"]", "[ \"no\" ]")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertFalse(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
            // Two items are not one switch value, so the setting hides.
            Files.writeString(file, "hide_inputs = [\"on\", \"off\"]\n");
            assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs());
        } finally { Files.deleteIfExists(file); }
    }
    /** A one-line triple-quoted string is not decoded, so a hide key set to it hides, even for "off". The e2e test checks the warning. */
    @Test void configFileOneLineTripleQuotedValueOnAHideKeyHides() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-triple-", ".toml");
        try {
            for (String value : List.of("\"\"\"off\"\"\"", "\"\"\"on\"\"\"")) {
                Files.writeString(file, "hide_inputs = " + value + "\n");
                assertTrue(AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).masking.isHideInputs(), value);
            }
        } finally { Files.deleteIfExists(file); }
    }
    /** Only hide keys read a single-quoted string as its text; any other setting keeps its value as written. */
    @Test void singleQuotedValueOfAnotherSettingKeepsItsQuotes() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-quoted-", ".toml");
        try {
            Files.writeString(file, "project_name = 'quoted-project'\n");
            assertEquals("'quoted-project'", AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).get("FI_PROJECT_NAME"));
            Files.writeString(file, "project_name = \"double-quoted-project\"\n");
            assertEquals("double-quoted-project", AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).get("FI_PROJECT_NAME"));
        } finally { Files.deleteIfExists(file); }
    }
    /** FI_HIDE_ERROR_DETAILS hides the provider's error text and nothing else. */
    @ParameterizedTest @ValueSource(strings = {"true", "on", "1", "maybe"})
    void errorDetailsSwitchHidesTheErrorTextAndNothingElse(String value) {
        AutoConfig config = env("FI_HIDE_ERROR_DETAILS", value);
        assertTrue(config.masking.isHideErrorDetails(), value);
        for (String key : ERROR_KEYS) assertTrue(TraceAIAuto.hidden(key, null, config), value + " " + key);
        for (String key : List.of("input.value", "output.value", "gen_ai.input.messages.0.message.content", "gen_ai.output.messages.0.message.content", "llm.invocation_parameters", "http.response.status_code"))
            assertFalse(TraceAIAuto.hidden(key, null, config), value + " " + key);
        assertFalse(config.masking.isHideInputs()); assertFalse(config.masking.isHideOutputs());
        assertFalse(TraceAIAuto.hidesContent(config), "error details are not content");
    }
    @ParameterizedTest @ValueSource(strings = {"false", "off", "no", "0"})
    void errorDetailsSwitchOffShowsTheErrorText(String value) {
        AutoConfig config = env("FI_HIDE_ERROR_DETAILS", value);
        assertFalse(config.masking.isHideErrorDetails(), value);
        for (String key : ERROR_KEYS) assertFalse(TraceAIAuto.hidden(key, null, config), value + " " + key);
    }
    /** An input or output content switch still hides the error text, as before. */
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT"})
    void aContentSwitchStillHidesTheErrorText(String name) {
        AutoConfig config = env(name, "true");
        for (String key : ERROR_KEYS) assertTrue(TraceAIAuto.hidden(key, null, config), name + " " + key);
        assertFalse(config.masking.isHideErrorDetails(), "the content switch is not the error details switch");
    }
    /** The status description is withheld by the error switch and by each of the eight content switches, and by nothing else. */
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_ERROR_DETAILS", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_INPUT_IMAGES", "FI_HIDE_LLM_INVOCATION_PARAMETERS"})
    void everyContentSwitchAndTheErrorSwitchHideTheErrorText(String name) {
        assertTrue(TraceAIAuto.hidesErrorText(env(name, "true")), name);
    }
    @Test void noSwitchLeavesTheErrorTextVisible() {
        assertFalse(TraceAIAuto.hidesErrorText(env("FI_HIDE_ERROR_DETAILS", "false")));
        assertFalse(TraceAIAuto.hidesErrorText(env("FI_SAMPLE_RATE", "1")));
    }
    /**
     * The warning prints the file's path through the same masking as the doctor report: a credential the agent knows
     * is never printed, even inside a path.
     */
    @Test void configFileWarningMasksAKnownCredentialInThePath() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-SYNTH_KEY_PLACEHOLDER-", ".toml");
        try {
            Files.write(file, "hide_inputs = true # note café\n".getBytes(java.nio.charset.StandardCharsets.ISO_8859_1));
            var output = new java.io.ByteArrayOutputStream(); var stderr = System.err;
            try (var captured = new java.io.PrintStream(output, true, java.nio.charset.StandardCharsets.UTF_8)) {
                System.setErr(captured);
                AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of("FI_API_KEY", "SYNTH_KEY_PLACEHOLDER", "FI_SECRET_KEY", "SYNTH_SECRET_PLACEHOLDER"));
            } finally { System.setErr(stderr); }
            String text = output.toString(java.nio.charset.StandardCharsets.UTF_8);
            List<String> warnings = text.lines().filter(line -> line.contains("] Config file ")).toList();
            assertEquals(1, warnings.size(), text);
            assertTrue(warnings.get(0).contains("not valid UTF-8"), text);
            assertFalse(text.contains("SYNTH_KEY_PLACEHOLDER"), "the credential in the path is masked");
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
            List<String> warnings = output.lines().filter(line -> line.contains("] Config file ")).toList();
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
            List<String> warnings = output.lines().filter(line -> line.contains("] Config file ")).toList();
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
