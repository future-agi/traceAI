package ai.traceai.annotations;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Traces the annotated method as a span of kind {@code AGENT}. The span records the method's arguments
 * as {@code input.value}, its return value as {@code output.value} and any exception, which is
 * rethrown unchanged. Spans are created only while the TraceAI Java agent is active (see "Trace your own methods" in the Java README).
 */
@Documented
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface Agent {
    /** Span name; the method name when empty. */
    String name() default "";
}
