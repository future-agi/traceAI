package ai.traceai.semantickernel;

import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.builders.SemanticKernelBuilder;
import com.microsoft.semantickernel.orchestration.FunctionInvocation;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import reactor.core.publisher.Mono;

/** Keeps the 1.3 ABI off classes that must load on both sides of the 1.4 API break. */
final class KernelInvocationCompat {
    private static final Method LEGACY_ARGUMENTS = legacyArguments();

    private KernelInvocationCompat() { }

    static boolean isLegacy() {
        return LEGACY_ARGUMENTS != null;
    }

    static KernelFunctionArguments emptyArguments() {
        // The concrete Builder.build return descriptor changed in 1.4; the interface's
        // erased Object descriptor remains available on both sides of that break.
        SemanticKernelBuilder<?> builder = KernelFunctionArguments.builder();
        return (KernelFunctionArguments) builder.build();
    }

    static <T> Mono<FunctionResult<T>> invoke(Kernel kernel, KernelFunction<T> function,
            KernelFunctionArguments arguments, TracedInvocation invocation) {
        if (!isLegacy()) {
            return Modern.invoke(kernel, function, arguments, invocation);
        }
        // Semantic Kernel 1.3 has neither invocation-scoped telemetry nor the KernelArguments
        // base class. Its old withArguments descriptor is looked up only on this runtime.
        FunctionInvocation<T> call = kernel.invokeAsync(function);
        try {
            @SuppressWarnings("unchecked")
            Mono<FunctionResult<T>> result = (Mono<FunctionResult<T>>) LEGACY_ARGUMENTS.invoke(call, arguments);
            return result;
        } catch (IllegalAccessException e) {
            throw new IllegalStateException("Cannot invoke Semantic Kernel 1.3 withArguments", e);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause();
            if (cause instanceof RuntimeException) throw (RuntimeException) cause;
            if (cause instanceof Error) throw (Error) cause;
            throw new IllegalStateException("Semantic Kernel invocation failed", cause);
        }
    }

    private static Method legacyArguments() {
        try {
            return FunctionInvocation.class.getMethod("withArguments", KernelFunctionArguments.class);
        } catch (NoSuchMethodException ignored) {
            return null;
        }
    }

    /** Loaded only when KernelArguments and invocation-scoped telemetry are present (1.4.5+). */
    private static final class Modern {
        static <T> Mono<FunctionResult<T>> invoke(Kernel kernel, KernelFunction<T> function,
                KernelFunctionArguments arguments, TracedInvocation invocation) {
            invocation.replaceRootFunctionSpan();
            return kernel.invokeAsync(function).withArguments(arguments).withTelemetry(invocation.telemetry());
        }
    }
}
