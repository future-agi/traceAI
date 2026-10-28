package ai.traceai.spring.autoconfigure;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

import static org.assertj.core.api.Assertions.assertThat;

/** Fresh JVM: disabled auto-configuration must not create a tracer or touch the global registration. */
class TraceAIDisabledTest {

    @Test
    void disabledCreatesNoTracerBeanAndDoesNotInitializeTheGlobalTracer() {
        new ApplicationContextRunner()
            .withConfiguration(AutoConfigurations.of(TraceAIAutoConfiguration.class))
            .withPropertyValues("traceai.enabled=false")
            .run(context -> {
                assertThat(context).doesNotHaveBean(FITracer.class);
                assertThat(TraceAI.isInitialized()).isFalse();
                assertThat(context).hasNotFailed();
            });
    }
}
