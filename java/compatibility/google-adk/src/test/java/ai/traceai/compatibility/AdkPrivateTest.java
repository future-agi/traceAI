package ai.traceai.compatibility;

import org.junit.jupiter.api.Test;

class AdkPrivateTest {
    @Test void nativeRunnerIsPrivateOnTheWireByDefault() throws Exception {
        AdkWireContract.run(false);
    }
}
