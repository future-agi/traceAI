package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import io.opentelemetry.api.trace.Span;
import reactor.core.publisher.Mono;

import java.util.Map;

/**
 * Instrumentation wrapper for Microsoft Semantic Kernel.
 * Wraps the Kernel to provide automatic tracing of function invocations.
 *
 * <p>Usage:</p>
 * <pre>
 * Kernel kernel = Kernel.builder()
 *     .withAIService(ChatCompletionService.class, chatService)
 *     .build();
 *
 * TracedKernel tracedKernel = new TracedKernel(kernel);
 *
 * FunctionResult&lt;String&gt; result = tracedKernel.invokeAsync(
 *     myFunction,
 *     KernelFunctionArguments.builder()
 *         .withVariable("input", "Hello")
 *         .build()
 * ).block();
 * </pre>
 */
public class TracedKernel {

    private static final String LLM_SYSTEM = "semantic-kernel";
    private static final String SEMANTIC_KERNEL_FUNCTION_NAME = "semantic_kernel.function_name";
    private static final String SEMANTIC_KERNEL_PLUGIN_NAME = "semantic_kernel.plugin_name";

    private final Kernel kernel;
    private final FITracer tracer;

    /**
     * Creates a new traced Kernel with the given kernel and tracer.
     *
     * @param kernel the Semantic Kernel to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedKernel(Kernel kernel, FITracer tracer) {
        this.kernel = kernel;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Kernel using the global TraceAI tracer.
     *
     * @param kernel the Semantic Kernel to wrap
     */
    public TracedKernel(Kernel kernel) {
        this(kernel, TraceAI.getTracer());
    }

    /**
     * Invokes a kernel function with tracing.
     *
     * <p>The span starts when the returned {@code Mono} is subscribed and ends when that
     * subscription succeeds, fails or is cancelled. Semantic Kernel's own model-call spans are
     * recorded under it; its duplicate function span is not.</p>
     *
     * @param function the kernel function to invoke
     * @param arguments the function arguments
     * @param <T> the result type
     * @return a Mono containing the function result
     */
    public <T> Mono<FunctionResult<T>> invokeAsync(KernelFunction<T> function, KernelFunctionArguments arguments) {
        String functionName = function.getName();
        String pluginName = function.getPluginName();

        return ReactiveTracing.mono(tracer, buildSpanName(pluginName, functionName), FISpanKind.AGENT, false,
            invocation -> {
                Span span = invocation.span();
                setFunctionAttributes(span, functionName, pluginName);
                captureInputArguments(span, arguments);
                return KernelInvocationCompat.invoke(kernel, function, arguments, invocation);
            },
            (invocation, result) -> captureOutput(invocation.span(), result));
    }

    /**
     * Invokes a kernel function with default arguments.
     *
     * @param function the kernel function to invoke
     * @param <T> the result type
     * @return a Mono containing the function result
     */
    public <T> Mono<FunctionResult<T>> invokeAsync(KernelFunction<T> function) {
        return invokeAsync(function, KernelInvocationCompat.emptyArguments());
    }

    /**
     * Invokes a prompt directly with tracing.
     *
     * @param prompt the prompt to execute
     * @param arguments the function arguments
     * @return a Mono containing the function result
     */
    public Mono<FunctionResult<String>> invokePromptAsync(String prompt, KernelFunctionArguments arguments) {
        return ReactiveTracing.mono(tracer, "Semantic Kernel Prompt", FISpanKind.AGENT, false,
            invocation -> {
                Span span = invocation.span();
                setFunctionAttributes(span, "prompt", null);
                tracer.setInputValue(span, prompt);
                captureInputArguments(span, arguments);

                KernelFunction<String> promptFunction = KernelFunction.<String>createFromPrompt(prompt)
                    .build();
                return KernelInvocationCompat.invoke(kernel, promptFunction, arguments, invocation);
            },
            (invocation, result) -> captureOutput(invocation.span(), result));
    }

    /**
     * Invokes a prompt directly with tracing using default arguments.
     *
     * @param prompt the prompt to execute
     * @return a Mono containing the function result
     */
    public Mono<FunctionResult<String>> invokePromptAsync(String prompt) {
        return invokePromptAsync(prompt, KernelInvocationCompat.emptyArguments());
    }

    /**
     * Gets the underlying Semantic Kernel.
     *
     * @return the wrapped Kernel
     */
    public Kernel unwrap() {
        return kernel;
    }

    /**
     * Gets the tracer being used.
     *
     * @return the FITracer
     */
    public FITracer getTracer() {
        return tracer;
    }

    private String buildSpanName(String pluginName, String functionName) {
        if (pluginName != null && !pluginName.isEmpty()) {
            return "Semantic Kernel: " + pluginName + "." + functionName;
        }
        return "Semantic Kernel: " + functionName;
    }

    private void captureInputArguments(Span span, KernelFunctionArguments arguments) {
        if (arguments == null) {
            return;
        }

        StringBuilder inputBuilder = new StringBuilder();
        for (Map.Entry<String, ?> entry : arguments.entrySet()) {
            if (inputBuilder.length() > 0) {
                inputBuilder.append("\n");
            }
            inputBuilder.append(entry.getKey()).append(": ");
            Object value = entry.getValue();
            if (value != null) {
                inputBuilder.append(value.toString());
            }
        }

        if (inputBuilder.length() > 0) {
            tracer.setInputValue(span, inputBuilder.toString());
        }

        // Also capture raw input
        tracer.setRawInput(span, arguments);
    }

    private void setFunctionAttributes(Span span, String functionName, String pluginName) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
        span.setAttribute(SEMANTIC_KERNEL_FUNCTION_NAME, functionName);
        if (pluginName != null && !pluginName.isEmpty()) {
            span.setAttribute(SEMANTIC_KERNEL_PLUGIN_NAME, pluginName);
        }
    }

    private <T> void captureOutput(Span span, FunctionResult<T> result) {
        if (result == null) {
            return;
        }

        T value = result.getResult();
        if (value != null) {
            tracer.setOutputValue(span, value.toString());
        }

        // Token usage belongs to the model-call span below this one. Copying it here would count
        // the same tokens twice.
        tracer.setRawOutput(span, result);
    }
}
