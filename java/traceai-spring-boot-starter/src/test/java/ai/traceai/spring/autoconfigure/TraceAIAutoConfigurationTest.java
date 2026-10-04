package ai.traceai.spring.autoconfigure;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.spring.TracedChatModel;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Starter behaviour that does not touch the process-wide OpenTelemetry registration.
 * Each case uses a fresh context; no exporter endpoint or credentials are configured.
 */
class TraceAIAutoConfigurationTest {

    private final java.util.concurrent.atomic.AtomicInteger contexts = new java.util.concurrent.atomic.AtomicInteger();

    /** A unique property per case defeats ApplicationContextRunner's context cache, so no case reuses another's initialized tracer. */
    private ApplicationContextRunner runner() {
        return new ApplicationContextRunner()
            .withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class))
            .withPropertyValues("traceai.project-name=unit-" + contexts.incrementAndGet());
    }

    @Test
    void enabledByDefaultCreatesATracerBean() {
        runner().run(context -> {
            assertThat(context).hasSingleBean(FITracer.class);
            assertThat(TraceAI.isInitialized()).isTrue();
        });
    }

    @Test
    void explicitEnabledTrueCreatesATracerBean() {
        runner().withPropertyValues("traceai.enabled=true", "traceai.project-name=unit").run(context -> {
            assertThat(context).hasSingleBean(FITracer.class);
            assertThat(context.getBean(FITracer.class)).isSameAs(TraceAI.getTracer());
        });
    }

    @Test
    void autoConfigurationDoesNotWrapUserModels() {
        runner().withUserConfiguration(ChatModelConfiguration.class).run(context -> {
            assertThat(context).hasSingleBean(ChatModel.class);
            assertThat(context.getBean(ChatModel.class)).isInstanceOf(PlainChatModel.class);
        });
    }

    @Test
    void manualWrappingUsesTheExplicitTracer() {
        runner().run(context -> {
            FITracer tracer = context.getBean(FITracer.class);
            PlainChatModel delegate = new PlainChatModel();
            TracedChatModel wrapped = new TracedChatModel(delegate, tracer, "synthetic");
            assertThat(wrapped.unwrap()).isSameAs(delegate);
            assertThat(wrapped.call(new Prompt("hi"))).isNull();
        });
    }

    @Configuration
    static class ChatModelConfiguration {
        @Bean
        ChatModel chatModel() {
            return new PlainChatModel();
        }
    }

    static final class PlainChatModel implements ChatModel {
        @Override
        public ChatResponse call(Prompt prompt) {
            return null;
        }
    }
}
