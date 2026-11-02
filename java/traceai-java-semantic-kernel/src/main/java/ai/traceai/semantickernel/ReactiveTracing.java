package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextStorage;
import io.opentelemetry.context.Scope;
import java.util.Objects;
import java.util.function.BiConsumer;
import java.util.function.Function;
import java.util.function.Supplier;
import org.reactivestreams.Publisher;
import reactor.core.Exceptions;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.publisher.SignalType;
import reactor.util.context.ContextView;

/**
 * Subscription-scoped spans for Semantic Kernel's lazy Reactor publishers.
 *
 * <p>Each subscription starts one span, under the OpenTelemetry context carried by Reactor or the
 * subscribing thread. The span is current while the operation is assembled and subscribed, is
 * stored in the Reactor context for deferred work, and ends once on success, error or
 * cancellation. Nothing starts, and the delegate is not called, until the result is subscribed.</p>
 */
final class ReactiveTracing {

    static final AttributeKey<Boolean> CANCELLED = AttributeKey.booleanKey("semantic_kernel.cancelled");

    static {
        // Use the maintained OTel bridge for scheduler hops when Semantic Kernel brings it.
        ReactorOtelContext.bridged();
    }

    private ReactiveTracing() { }

    static <T> Mono<T> mono(FITracer tracer, String name, FISpanKind kind, boolean modelFallback,
            Function<TracedInvocation, Mono<T>> operation, BiConsumer<TracedInvocation, T> onSuccess) {
        Objects.requireNonNull(operation, "operation");
        return Mono.deferContextual(view -> {
            TracedInvocation invocation = start(tracer, name, kind, modelFallback, view);
            Mono<T> source = assemble(invocation, () -> operation.apply(invocation));
            return Mono.<T>fromDirect(subscribeWithin(source, invocation.context()))
                .contextWrite(reactorContext -> ReactorOtelContext.store(reactorContext, invocation.context()))
                .doOnSuccess(value -> {
                    try {
                        onSuccess.accept(invocation, value);
                    } catch (RuntimeException ignored) {
                        // Instrumentation must not change the application's result.
                    }
                    invocation.span().setStatus(StatusCode.OK);
                    invocation.finish(SignalType.ON_COMPLETE);
                })
                .doOnError(error -> {
                    tracer.setError(invocation.span(), error);
                    invocation.finish(SignalType.ON_ERROR);
                })
                .doOnCancel(() -> {
                    invocation.span().setAttribute(CANCELLED, true);
                    invocation.finish(SignalType.CANCEL);
                })
                .doFinally(invocation::finish);
        });
    }

    static <T> Flux<T> flux(FITracer tracer, String name, FISpanKind kind, boolean modelFallback,
            Function<TracedInvocation, Flux<T>> operation) {
        Objects.requireNonNull(operation, "operation");
        return Flux.deferContextual(view -> {
            TracedInvocation invocation = start(tracer, name, kind, modelFallback, view);
            Flux<T> source = assemble(invocation, () -> operation.apply(invocation), Flux::error);
            return Flux.<T>from(subscribeWithin(source, invocation.context()))
                .contextWrite(reactorContext -> ReactorOtelContext.store(reactorContext, invocation.context()))
                .doOnComplete(() -> {
                    invocation.span().setStatus(StatusCode.OK);
                    invocation.finish(SignalType.ON_COMPLETE);
                })
                .doOnError(error -> {
                    tracer.setError(invocation.span(), error);
                    invocation.finish(SignalType.ON_ERROR);
                })
                .doOnCancel(() -> {
                    invocation.span().setAttribute(CANCELLED, true);
                    invocation.finish(SignalType.CANCEL);
                })
                .doFinally(invocation::finish);
        });
    }

    private static TracedInvocation start(FITracer tracer, String name, FISpanKind kind,
            boolean modelFallback, ContextView view) {
        Context parent = ReactorOtelContext.read(view, Context.current());
        try (Scope ignored = parent.makeCurrent()) {
            Span span = tracer.startSpan(name, kind, parent);
            Context context = parent.with(span);
            return new TracedInvocation(tracer, span, context, modelFallback);
        }
    }

    private static <T> Mono<T> assemble(TracedInvocation invocation, Supplier<Mono<T>> operation) {
        return assemble(invocation, operation, Mono::error);
    }

    private static <P> P assemble(TracedInvocation invocation, Supplier<P> operation,
            Function<Throwable, P> failure) {
        try {
            P source = callWithin(invocation.context(), operation);
            return source != null ? source : failure.apply(
                new NullPointerException("Semantic Kernel returned a null publisher"));
        } catch (Throwable error) {
            Exceptions.throwIfFatal(error);
            // Report synchronous delegate failures through the publisher, after the span exists.
            return failure.apply(error);
        }
    }

    /** Subscribes with the span current, then restores the subscribing thread's context. */
    private static <T> Publisher<T> subscribeWithin(Publisher<T> source, Context context) {
        return subscriber -> callWithin(context, () -> {
            source.subscribe(subscriber);
            return null;
        });
    }

    private static <T> T callWithin(Context context, Supplier<T> operation) {
        Context before = Context.current();
        Scope scope = context.makeCurrent();
        try {
            return operation.get();
        } finally {
            scope.close();
            if (Context.current() != before) {
                // Semantic Kernel can leave its own scope open on this thread. Restore the
                // caller's context so later work is not parented to an ended span.
                @SuppressWarnings({"MustBeClosedChecker", "unused"})
                Scope restored = ContextStorage.get().attach(before);
            }
        }
    }
}
