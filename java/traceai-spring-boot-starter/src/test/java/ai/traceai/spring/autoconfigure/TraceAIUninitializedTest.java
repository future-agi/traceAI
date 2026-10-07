package ai.traceai.spring.autoconfigure;

import ai.traceai.TraceAI;
import ai.traceai.spring.TracedChatModel;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Fresh JVM: the two-argument wrapper must fail closed when nobody initialized TraceAI. */
class TraceAIUninitializedTest {

    @Test
    void twoArgConstructorFailsWhenTheGlobalTracerWasNeverInitialized() {
        assertThat(TraceAI.isInitialized()).isFalse();
        assertThatThrownBy(() -> new TracedChatModel(new ChatModel() {
            @Override
            public ChatResponse call(Prompt prompt) {
                return null;
            }
        }, "synthetic"))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("not initialized");
    }
}
