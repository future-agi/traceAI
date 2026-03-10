package ai.traceai.langchain4j;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.Arrays;

/**
 * Factory for creating traced proxies of LangChain4j AiServices.
 * Wraps AiService interfaces to provide automatic tracing of all method calls.
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

        // Skip tracing for Object methods
        if (method.getDeclaringClass() == Object.class) {
            return method.invoke(target, args);
        }

        String spanName = "AiService." + serviceInterface.getSimpleName() + "." + method.getName();
        Span span = tracer.startSpan(spanName, FISpanKind.AGENT);

        try (Scope scope = span.makeCurrent()) {
            // Set service attributes
            span.setAttribute("langchain4j.service", serviceInterface.getSimpleName());
            span.setAttribute("langchain4j.method", method.getName());

            // Capture input parameters
            if (args != null && args.length > 0) {
                StringBuilder inputBuilder = new StringBuilder();
                String[] paramNames = getParameterNames(method);

                for (int i = 0; i < args.length; i++) {
                    if (i > 0) inputBuilder.append(", ");
                    inputBuilder.append(paramNames[i]).append("=");
                    inputBuilder.append(args[i] != null ? args[i].toString() : "null");
                }

                tracer.setInputValue(span, inputBuilder.toString());

                // Set individual parameter attributes
                for (int i = 0; i < args.length; i++) {
                    if (args[i] != null) {
                        span.setAttribute("langchain4j.param." + paramNames[i], args[i].toString());
                    }
                }
            }

            // Invoke the method
            Object result = method.invoke(target, args);

            // Capture output
            if (result != null) {
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
            span.end();
        }
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
