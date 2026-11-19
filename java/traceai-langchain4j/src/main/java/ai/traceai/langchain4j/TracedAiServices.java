package ai.traceai.langchain4j;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.Flow;

/**
 * Factory for creating traced proxies of LangChain4j AiServices.
 * Traces synchronous and CompletionStage-returning service methods. Lazy TokenStream
 * and Flow.Publisher methods are delegated unchanged without a service span: creating
 * a stream handle is not completing a service invocation. Attach LangChain4jListener
 * to the streaming model for model-level spans; this proxy does not observe tool execution.
 *
 * <p>Usage:</p>
 * <pre>
 * interface Assistant {
 *     String chat(String message);
 * }
 *
 * ChatLanguageModel model = OpenAiChatModel.builder()
 *     .apiKey("...")
 *     .build();
 *
 * Assistant assistant = TracedAiServices.create(Assistant.class,
 *     AiServices.builder(Assistant.class)
 *         .chatLanguageModel(model)
 *         .build()
 * );
 *
 * String response = assistant.chat("Hello!");
 * </pre>
 */
public final class TracedAiServices {

    private TracedAiServices() {
        throw new UnsupportedOperationException("Utility class cannot be instantiated");
    }

    /**
     * Creates a traced proxy for an AiService interface.
     *
     * @param serviceInterface the AiService interface class
     * @param service          the built AiService instance
     * @param <T>              the service interface type
     * @return a traced proxy of the service
     */
    public static <T> T create(Class<T> serviceInterface, T service) {
        return create(serviceInterface, service, TraceAI.getTracer());
    }

    /**
     * Creates a traced proxy for an AiService interface with a custom tracer.
     *
     * @param serviceInterface the AiService interface class
     * @param service          the built AiService instance
     * @param tracer           the FITracer for instrumentation
     * @param <T>              the service interface type
     * @return a traced proxy of the service
     */
    @SuppressWarnings("unchecked")
    public static <T> T create(Class<T> serviceInterface, T service, FITracer tracer) {
        return (T) Proxy.newProxyInstance(
            serviceInterface.getClassLoader(),
            new Class<?>[]{serviceInterface},
            (proxy, method, args) -> invokeWithTracing(service, serviceInterface, method, args, tracer)
        );
    }

    private static Object invokeWithTracing(
            Object target,
            Class<?> serviceInterface,
            Method method,
            Object[] args,
            FITracer tracer) throws Throwable {

        // AiServices permits package-private interfaces in application code.
        if (!method.canAccess(target)) method.trySetAccessible();

        // Do not report lazy handle construction as a successful service invocation.
        if (method.getDeclaringClass() == Object.class
                || Flow.Publisher.class.isAssignableFrom(method.getReturnType())
                || isTokenStream(method.getReturnType())) {
            try {
                return method.invoke(target, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        }

        String spanName = "AiService." + serviceInterface.getSimpleName() + "." + method.getName();
        Span span = tracer.startSpan(spanName, FISpanKind.AGENT);
        boolean deferred = false;

        try (Scope scope = span.makeCurrent()) {
            // Set service attributes
            span.setAttribute("langchain4j.service", serviceInterface.getSimpleName());
            span.setAttribute("langchain4j.method", method.getName());

            // Capture input parameters
            if (!tracer.getConfig().isHideInputs() && args != null && args.length > 0) {
                StringBuilder inputBuilder = new StringBuilder();
                String[] paramNames = getParameterNames(method);

                for (int i = 0; i < args.length; i++) {
                    if (i > 0) inputBuilder.append(", ");
                    inputBuilder.append(paramNames[i]).append("=");
                    inputBuilder.append(args[i] != null ? args[i].toString() : "null");
                }

                tracer.setInputValue(span, inputBuilder.toString());
            }

            // Invoke the method
            Object result = method.invoke(target, args);

            if (result instanceof CompletionStage<?> stage) {
                stage.whenComplete((value, error) -> {
                    try {
                        if (error != null) {
                            Throwable cause = error instanceof CompletionException && error.getCause() != null
                                    ? error.getCause() : error;
                            tracer.setError(span, cause);
                        } else {
                            if (value != null && !tracer.getConfig().isHideOutputs())
                                tracer.setOutputValue(span, value.toString());
                            span.setStatus(StatusCode.OK);
                        }
                    } finally {
                        span.end();
                    }
                });
                deferred = true;
                return result; // Preserve identity and cancellation semantics of the original future.
            }

            // Capture output
            if (result != null && !tracer.getConfig().isHideOutputs()) {
                tracer.setOutputValue(span, result.toString());
            }

            span.setStatus(StatusCode.OK);
            return result;

        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause();
            tracer.setError(span, cause);
            throw cause;
        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            if (!deferred) span.end();
        }
    }

    private static boolean isTokenStream(Class<?> type) {
        if (type == null) return false;
        // TokenStream lives in the optional langchain4j service module, not core.
        if (type.getName().equals("dev.langchain4j.service.TokenStream")) return true;
        for (Class<?> parent : type.getInterfaces()) {
            if (isTokenStream(parent)) return true;
        }
        return isTokenStream(type.getSuperclass());
    }

    private static String[] getParameterNames(Method method) {
        // Try to get actual parameter names (requires -parameters compiler flag)
        String[] names = new String[method.getParameterCount()];
        var parameters = method.getParameters();

        for (int i = 0; i < parameters.length; i++) {
            if (parameters[i].isNamePresent()) {
                names[i] = parameters[i].getName();
            } else {
                names[i] = "arg" + i;
            }
        }

        return names;
    }
}
