package ai.traceai.spring.compatibility;

import ai.traceai.FITracer;
import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.TracedChatModel;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;

import static org.junit.jupiter.api.Assertions.*;

/** Calls the actual 2.x interface, not a reflected method on the wrapper class. */
class ModernChatOptionsTest {
    @Test
    void modernAndLegacyAccessorsDelegateIndependentlyWithoutCopying() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            ChatOptions modern = ChatOptions.builder().model("modern-model").build();
            ChatOptions legacy = ChatOptions.builder().model("legacy-model").build();
            ChatModel delegate = new ChatModel() {
                @Override public ChatResponse call(Prompt prompt) { throw new AssertionError("not a model call"); }
                @Override public ChatOptions getOptions() { return modern; }
                @Override public ChatOptions getDefaultOptions() { return legacy; }
            };
            ChatModel wrapper = new TracedChatModel(delegate, new FITracer(f.sdk.get("options-contract")), "synthetic");
            assertSame(modern, wrapper.getOptions());
            assertSame(legacy, wrapper.getDefaultOptions());
            assertTrue(f.spans().isEmpty());
        }
    }

    @Test
    void modernAccessorPreservesNullAndExceptionIdentity() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var failure = new IllegalStateException("synthetic options failure");
            var fatal = new AssertionError("synthetic options error");
            for (Throwable thrown : new Throwable[]{null, failure, fatal}) {
                ChatModel delegate = new ChatModel() {
                    @Override public ChatResponse call(Prompt prompt) { throw new AssertionError("not a model call"); }
                    @Override public ChatOptions getOptions() {
                        if (thrown instanceof RuntimeException e) throw e;
                        if (thrown instanceof Error e) throw e;
                        return null;
                    }
                };
                ChatModel wrapper = new TracedChatModel(delegate, new FITracer(f.sdk.get("options-contract")), "synthetic");
                if (thrown == null) assertNull(wrapper.getOptions());
                else assertSame(thrown, assertThrows(thrown.getClass(), wrapper::getOptions));
            }
            assertTrue(f.spans().isEmpty());
        }
    }
}
