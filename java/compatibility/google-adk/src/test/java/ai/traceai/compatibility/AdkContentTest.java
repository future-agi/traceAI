package ai.traceai.compatibility;

import org.junit.jupiter.api.Test;

class AdkContentTest {
    @Test void optedInNativeRunnerHasStructuredMessagesAndToolsOnTheWire() throws Exception {
        AdkWireContract.run(true);
    }
}
