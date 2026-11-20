package ai.traceai.cohere;

import com.cohere.api.requests.ChatStreamRequest;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class CohereSurfaceTest {
    @Test
    void exposesTracedV2Facade() throws Exception {
        assertThat(TracedCohereClient.class.getMethod("v2").getReturnType().getSimpleName())
            .isEqualTo("TracedCohereV2Client");
    }

    @Test
    void exposesCloseableV1Stream() throws Exception {
        assertThat(AutoCloseable.class.isAssignableFrom(TracedCohereClient.class
            .getMethod("chatStream", ChatStreamRequest.class).getReturnType())).isTrue();
    }
}
