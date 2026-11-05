package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationHandler;
import io.micrometer.observation.ObservationRegistry;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.model.tool.ToolCallingManager;
import org.springframework.ai.tool.execution.ToolExecutionException;
import org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor;
import org.springframework.ai.tool.observation.DefaultToolCallingObservationConvention;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;
import org.springframework.ai.tool.observation.ToolCallingObservationConvention;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import static org.assertj.core.api.Assertions.assertThat;

@Timeout(30)
class ConcreteToolPolicyBeanTest {
    @ParameterizedTest @ValueSource(booleans = {false, true})
    void concreteTypedConsumerKeepsTheOriginalPolicyWithAndWithoutTracing(boolean tracing) throws Exception {
        try (var server = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            server.reply(ToolExecutionFailureTest.TOOL_REPLY);
            server.reply(NativeSpringAITest.CHAT);
            var runner = new ApplicationContextRunner().withUserConfiguration(ExistingApplication.class)
                .withPropertyValues("management.otlp.tracing.export.enabled=false",
                    "management.opentelemetry.tracing.export.otlp.enabled=false");
            if (tracing) runner = runner.withUserConfiguration(DocumentedBootSetupTest.Application.class);
            runner.run(context -> {
                assertThat(context).hasNotFailed().hasSingleBean(CustomPolicy.class)
                    .hasSingleBean(ToolExecutionExceptionProcessor.class).hasSingleBean(ToolCallingManager.class);
                var policy = context.getBean(CustomPolicy.class);
                assertThat(context.getBean(ExistingConsumer.class).policy()).isSameAs(policy);
                assertThat(context.getBean(ToolExecutionExceptionProcessor.class)).isSameAs(policy);

                var registry = context.getBean(ObservationRegistry.class);
                var stopped = new AtomicReference<Observation.Context>();
                registry.observationConfig().observationHandler(new ObservationHandler<Observation.Context>() {
                    @Override public boolean supportsContext(Observation.Context value) {
                        return value instanceof ToolCallingObservationContext;
                    }
                    @Override public void onStop(Observation.Context value) { stopped.set(value); }
                });
                // Exercise a real failing tool through the manager, not just successful bean lookup.
                var prompt = ToolExecutionFailureTest.failingPrompt(policy.failure);
                assertThat(Models.toolChat(server.url(), registry, prompt, context.getBean(ToolCallingManager.class))
                    .getResult().getOutput().getText()).isEqualTo(NativeSpringAITest.OUTPUT);
                assertThat(policy.calls.get()).isEqualTo(1);
                assertThat(policy.received.get().getCause()).isSameAs(policy.failure);
                assertThat(server.requests).hasSize(2).anySatisfy(body -> assertThat(body).contains("custom recovery"));
                assertThat(stopped.get()).isNotNull();
                assertThat(stopped.get().getName()).isEqualTo("custom.tool.call");
                if (tracing) assertThat(stopped.get().getError()).isSameAs(policy.failure);
                else assertThat(stopped.get().getError()).isNull();
            });
        }
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    static class ExistingApplication {
        @Bean CustomPolicy customPolicy() { return new CustomPolicy(); }
        @Bean ExistingConsumer existingConsumer(CustomPolicy policy) { return new ExistingConsumer(policy); }
        @Bean ToolCallingObservationConvention customConvention() {
            return new DefaultToolCallingObservationConvention("custom.tool.call");
        }
    }

    static final class CustomPolicy implements ToolExecutionExceptionProcessor {
        final RuntimeException failure = new IllegalStateException("synthetic tool failure");
        final AtomicInteger calls = new AtomicInteger();
        final AtomicReference<ToolExecutionException> received = new AtomicReference<>();
        @Override public String process(ToolExecutionException exception) {
            calls.incrementAndGet();
            received.set(exception);
            return "custom recovery";
        }
    }

    record ExistingConsumer(CustomPolicy policy) {}
}
