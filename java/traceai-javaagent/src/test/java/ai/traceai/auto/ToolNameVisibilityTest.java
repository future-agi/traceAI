package ai.traceai.auto;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;

/** The tool name is the span's identity, not content: no hide switch withholds it, while the other gen_ai.tool.* keys follow the input switches. */
class ToolNameVisibilityTest {
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_ERROR_DETAILS"})
    void theToolNameIsNeverHidden(String flag) {
        AutoConfig config = AutoConfig.resolve(Map.of("config_file", "target/no-tool-name-config.toml"), Map.of(flag, "true"));
        assertFalse(TraceAIAuto.hidden("gen_ai.tool.name", "lookup", null, config), flag);
    }
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES"})
    void theToolDescriptionStillFollowsTheInputSwitches(String flag) {
        AutoConfig config = AutoConfig.resolve(Map.of("config_file", "target/no-tool-name-config.toml"), Map.of(flag, "true"));
        assertTrue(TraceAIAuto.hidden("gen_ai.tool.description", "d", null, config), flag);
    }
}
