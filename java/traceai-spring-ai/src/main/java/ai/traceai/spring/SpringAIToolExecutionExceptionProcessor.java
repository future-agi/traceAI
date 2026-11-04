package ai.traceai.spring;

import io.micrometer.observation.ObservationRegistry;
import java.util.Objects;
import org.springframework.ai.tool.execution.ToolExecutionException;
import org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;

/**
 * Records a failed native tool observation even when Spring AI converts the exception to a
 * tool-result message. The existing processor retains full control of recovery and rethrow policy.
 * No spans are created and neither the exception nor the returned tool result is changed.
 * Pass this decorator to the tool manager's builder, leaving the application's processor bean
 * unchanged so its concrete type, identity, and other consumers are preserved.
 */
public final class SpringAIToolExecutionExceptionProcessor implements ToolExecutionExceptionProcessor {
    private final ObservationRegistry registry;
    private final ToolExecutionExceptionProcessor delegate;

    /**
     * Wraps the application's existing exception policy using its tool manager's registry.
     * @param registry the registry used by the native tool calling manager
     * @param delegate the existing processor, not a replacement exception policy
     */
    public SpringAIToolExecutionExceptionProcessor(ObservationRegistry registry,
            ToolExecutionExceptionProcessor delegate) {
        this.registry = Objects.requireNonNull(registry, "registry");
        this.delegate = Objects.requireNonNull(delegate, "delegate");
    }

    @Override
    public String process(ToolExecutionException exception) {
        Objects.requireNonNull(exception, "exception");
        var observation = registry.getCurrentObservation();
        if (observation != null && observation.getContext() instanceof ToolCallingObservationContext) {
            // Spring AI invokes the processor inside the tool's observation.observe callback.
            // Record the original tool cause before a recovering delegate can swallow it.
            observation.error(exception.getCause() != null ? exception.getCause() : exception);
        }
        return delegate.process(exception);
    }
}
