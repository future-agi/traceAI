package ai.traceai.spring.autoconfigure;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import static org.assertj.core.api.Assertions.assertThat;

/** Fresh JVM: an application-supplied FITracer must win and the global tracer must stay uninitialized. */
class TraceAICustomTracerTest {

    @Test
    void existingCustomTracerBeanBacksOffWithoutInitializingTheGlobalTracer() {
        new ApplicationContextRunner()
            .withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class))
            .withUserConfiguration(CustomTracerConfiguration.class)
            .run(context -> {
                assertThat(context).hasSingleBean(FITracer.class);
                assertThat(context.getBean(FITracer.class)).isSameAs(CustomTracerConfiguration.CUSTOM);
                assertThat(TraceAI.isInitialized()).isFalse();
            });
    }

    @Configuration
    static class CustomTracerConfiguration {
        static final FITracer CUSTOM = new FITracer(io.opentelemetry.api.OpenTelemetry.noop().getTracer("custom"));

        @Bean
        FITracer fiTracer() {
            return CUSTOM;
        }
    }
}
