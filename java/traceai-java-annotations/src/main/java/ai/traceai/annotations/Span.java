package ai.traceai.annotations;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Traces the annotated method as a span with a custom kind. Use it for kinds without their own
 * annotation, such as {@code RETRIEVER}, {@code EMBEDDING}, {@code RERANKER}, {@code GUARDRAIL} or {@code WORKFLOW}.
 */
@Documented
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface Span {
    /** Span name; the method name when empty. */
    String name() default "";

    /** Span kind, case-insensitive. An empty or blank value means {@code CHAIN}. */
    String kind() default "CHAIN";

    /** Free-text description, recorded as the span description attribute when set. */
    String description() default "";
}
