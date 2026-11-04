package ai.traceai.spring;

import io.micrometer.observation.ObservationRegistry;
import java.util.Objects;
import java.util.function.Supplier;
import org.springframework.ai.tool.execution.ToolExecutionException;
import org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.config.BeanPostProcessor;

/**
 * Records a failed native tool observation even when Spring AI converts the exception to a
 * tool-result message. The existing processor retains full control of recovery and rethrow policy.
 * No spans are created and neither the exception nor the returned tool result is changed.
 */
public final class SpringAIToolExecutionExceptionProcessor implements ToolExecutionExceptionProcessor {
    private final Supplier<ObservationRegistry> registry;
    private final ToolExecutionExceptionProcessor delegate;

    /**
     * Wraps the application's existing exception policy using its tool manager's registry.
     * @param registry the registry used by the native tool calling manager
     * @param delegate the existing processor, not a replacement exception policy
     */
    public SpringAIToolExecutionExceptionProcessor(ObservationRegistry registry,
            ToolExecutionExceptionProcessor delegate) {
        this(() -> registry, delegate);
        Objects.requireNonNull(registry, "registry");
    }

    private SpringAIToolExecutionExceptionProcessor(Supplier<ObservationRegistry> registry,
            ToolExecutionExceptionProcessor delegate) {
        this.registry = registry;
        this.delegate = Objects.requireNonNull(delegate, "delegate");
    }

    @Override
    public String process(ToolExecutionException exception) {
        Objects.requireNonNull(exception, "exception");
        var observation = registry.get().getCurrentObservation();
        if (observation != null && observation.getContext() instanceof ToolCallingObservationContext) {
            // Spring AI invokes the processor inside the tool's observation.observe callback.
            // Record the original tool cause before a recovering delegate can swallow it.
            observation.error(exception.getCause() != null ? exception.getCause() : exception);
        }
        return delegate.process(exception);
    }

    /**
     * Decorates Spring-managed processors, including Spring AI's auto-configured processor, without
     * replacing their configured exception policy. Register as a {@code static @Bean}. Registry
     * resolution is deferred until execution so this post-processor cannot initialize tracing early.
     * Manually constructed tool managers must be given a decorated processor explicitly.
     * @param registries the application's observation registry provider
     * @return the post-processor to register with Spring
     */
    public static BeanPostProcessor beanPostProcessor(ObjectProvider<ObservationRegistry> registries) {
        Objects.requireNonNull(registries, "registries");
        return new BeanPostProcessor() {
            @Override
            public Object postProcessAfterInitialization(Object bean, String beanName) {
                if (bean instanceof ToolExecutionExceptionProcessor processor
                        && !(bean instanceof SpringAIToolExecutionExceptionProcessor)) {
                    return new SpringAIToolExecutionExceptionProcessor(
                        () -> registries.getIfUnique(() -> ObservationRegistry.NOOP), processor);
                }
                return bean;
            }
        };
    }
}
