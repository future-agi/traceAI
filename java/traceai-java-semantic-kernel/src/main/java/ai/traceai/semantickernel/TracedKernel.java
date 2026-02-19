package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.orchestration.PromptExecutionSettings;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
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
     * @param function the kernel function to invoke
     * @param arguments the function arguments
     * @param <T> the result type
     * @return a Mono containing the function result
     */
    public <T> Mono<FunctionResult<T>> invokeAsync(KernelFunction<T> function, KernelFunctionArguments arguments) {
        String functionName = function.getName();
        String pluginName = function.getPluginName();
        String spanName = buildSpanName(pluginName, functionName);

        Span span = tracer.startSpan(spanName, FISpanKind.AGENT);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute(SEMANTIC_KERNEL_FUNCTION_NAME, functionName);

            if (pluginName != null && !pluginName.isEmpty()) {
                span.setAttribute(SEMANTIC_KERNEL_PLUGIN_NAME, pluginName);
            }

            // Capture input arguments
            captureInputArguments(span, arguments);

            // Execute the function
            return kernel.invokeAsync(function)
                .withArguments(arguments)
                .doOnSuccess(result -> {
                    // Capture output
                    captureOutput(span, result);
                    span.setStatus(StatusCode.OK);
                })
                .doOnError(error -> {
                    tracer.setError(span, error);
                })
                .doFinally(signalType -> {
                    span.end();
                });
        }
    }

    /**
     * Invokes a kernel function with default arguments.
     *
     * @param function the kernel function to invoke
     * @param <T> the result type
     * @return a Mono containing the function result
     */
    public <T> Mono<FunctionResult<T>> invokeAsync(KernelFunction<T> function) {
        return invokeAsync(function, KernelFunctionArguments.builder().build());
    }

    /**
     * Invokes a prompt directly with tracing.
     *
     * @param prompt the prompt to execute
     * @param arguments the function arguments
     * @return a Mono containing the function result
     */
    public Mono<FunctionResult<String>> invokePromptAsync(String prompt, KernelFunctionArguments arguments) {
        Span span = tracer.startSpan("Semantic Kernel Prompt", FISpanKind.AGENT);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute(SEMANTIC_KERNEL_FUNCTION_NAME, "prompt");

            // Capture input
            tracer.setInputValue(span, prompt);
            captureInputArguments(span, arguments);

            // Create and execute inline prompt
            KernelFunction<String> promptFunction = KernelFunction.<String>createFromPrompt(prompt)
                .build();

            return kernel.invokeAsync(promptFunction)
                .withArguments(arguments)
                .doOnSuccess(result -> {
                    captureOutput(span, result);
                    span.setStatus(StatusCode.OK);
                })
                .doOnError(error -> {
                    tracer.setError(span, error);
                })
                .doFinally(signalType -> {
                    span.end();
                });
        }
    }

    /**
     * Invokes a prompt directly with tracing using default arguments.
     *
     * @param prompt the prompt to execute
     * @return a Mono containing the function result
     */
    public Mono<FunctionResult<String>> invokePromptAsync(String prompt) {
        return invokePromptAsync(prompt, KernelFunctionArguments.builder().build());
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

    private <T> void captureOutput(Span span, FunctionResult<T> result) {
        if (result == null) {
            return;
        }

        T value = result.getResult();
        if (value != null) {
            tracer.setOutputValue(span, value.toString());
        }

        // Capture raw output
        tracer.setRawOutput(span, result);

        // Capture metadata if available
        if (result.getMetadata() != null) {
            Map<String, Object> metadata = result.getMetadata();

            // Try to extract token usage if present
            if (metadata.containsKey("usage")) {
                Object usage = metadata.get("usage");
                extractTokenUsage(span, usage);
            }
        }
    }

    private void extractTokenUsage(Span span, Object usage) {
        if (usage == null) {
            return;
        }

        try {
            // Use reflection to extract token counts from usage object
            Integer promptTokens = extractField(usage, "promptTokens", Integer.class);
            Integer completionTokens = extractField(usage, "completionTokens", Integer.class);

            if (promptTokens != null && completionTokens != null) {
                int total = promptTokens + completionTokens;
                tracer.setTokenCounts(span, promptTokens, completionTokens, total);
            }
        } catch (Exception e) {
            // Ignore token extraction failures
        }
    }

    @SuppressWarnings("unchecked")
    private <T> T extractField(Object obj, String fieldName, Class<T> type) {
        if (obj == null) {
            return null;
        }

        try {
            // Try getter method
            String getterName = "get" + Character.toUpperCase(fieldName.charAt(0)) + fieldName.substring(1);
            try {
                Object value = obj.getClass().getMethod(getterName).invoke(obj);
                return type.isInstance(value) ? (T) value : null;
            } catch (NoSuchMethodException e) {
                // Try direct method
                try {
                    Object value = obj.getClass().getMethod(fieldName).invoke(obj);
                    return type.isInstance(value) ? (T) value : null;
                } catch (NoSuchMethodException e2) {
                    return null;
                }
            }
        } catch (Exception e) {
            return null;
        }
    }
}
