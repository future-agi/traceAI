package ai.traceai.semantickernel;

import ai.traceai.ContextAttributes;
import ai.traceai.FITracer;
import ai.traceai.FISpanKind;
import ai.traceai.SemanticConventions;
import com.microsoft.semantickernel.implementation.telemetry.SemanticKernelTelemetry;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import javax.annotation.Nullable;
import reactor.core.publisher.SignalType;

/** State for one subscription to a traced Semantic Kernel operation. */
final class TracedInvocation {
    private final FITracer tracer;
    private final Span span;
    private final Context context;
    private final Map<String, String> contextAttributes;
    private final Set<NativeTelemetry.InvocationSpan> open = ConcurrentHashMap.newKeySet();
    private final List<Consumer<SignalType>> finishers = new CopyOnWriteArrayList<>();
    private final AtomicBoolean rootFunctionPending = new AtomicBoolean();
    private final AtomicBoolean finished = new AtomicBoolean();
    private final boolean modelFallback;
    private volatile boolean nativeModelSeen;
    @Nullable private volatile SemanticKernelTelemetry telemetry;

    TracedInvocation(FITracer tracer, Span span, Context context) {
        this(tracer, span, context, false);
    }

    TracedInvocation(FITracer tracer, Span span, Context context, boolean modelFallback) {
        this.modelFallback = modelFallback;
        this.tracer = tracer;
        this.span = span;
        this.context = context;
        this.contextAttributes = ContextAttributes.getAttributesFromContext();
    }

    void nativeModelStarted() {
        nativeModelSeen = true;
        if (modelFallback) {
            // A service invocation can perform several model/tool rounds. Native spans own each
            // model call; the compatibility boundary owns content and cancellation, not tokens.
            span.setAttribute(SemanticConventions.FI_SPAN_KIND, FISpanKind.CHAIN.getValue());
            span.setAttribute(SemanticConventions.GEN_AI_SPAN_KIND, "chain");
        }
    }

    boolean hasNativeModel() {
        return nativeModelSeen;
    }

    FITracer tracer() {
        return tracer;
    }

    Span span() {
        return span;
    }

    Context context() {
        return context;
    }

    String traceId() {
        return span.getSpanContext().getTraceId();
    }

    Map<String, String> contextAttributes() {
        return contextAttributes;
    }

    /** Semantic Kernel telemetry that reports through this invocation. */
    SemanticKernelTelemetry telemetry() {
        SemanticKernelTelemetry current = telemetry;
        if (current == null) {
            synchronized (this) {
                current = telemetry;
                if (current == null) {
                    current = NativeTelemetry.create(this);
                    telemetry = current;
                }
            }
        }
        return current;
    }

    /** Passes the caller's invocation settings through, reporting Semantic Kernel spans here. */
    InvocationContext route(@Nullable InvocationContext invocationContext) {
        // 1.3 has no per-invocation telemetry; never load the newer telemetry adapter there.
        return KernelInvocationCompat.isLegacy() ? invocationContext
            : NativeTelemetry.route(invocationContext, this);
    }

    /** The wrapper span stands in for the next Semantic Kernel function span. */
    void replaceRootFunctionSpan() {
        rootFunctionPending.set(true);
    }

    boolean consumeRootFunction() {
        return rootFunctionPending.compareAndSet(true, false);
    }

    NativeTelemetry.InvocationSpan track(NativeTelemetry.InvocationSpan nativeSpan) {
        open.add(nativeSpan);
        // Cover both a span starting after cancellation and cancellation racing with registration.
        if (finished.get() && open.remove(nativeSpan)) {
            nativeSpan.endAbandoned();
        }
        return nativeSpan;
    }

    void untrack(NativeTelemetry.InvocationSpan nativeSpan) {
        open.remove(nativeSpan);
    }

    void onFinish(Consumer<SignalType> finisher) {
        finishers.add(finisher);
    }

    void finish(SignalType signal) {
        if (!finished.compareAndSet(false, true)) {
            return;
        }
        try {
            for (Consumer<SignalType> finisher : finishers) {
                try {
                    finisher.accept(signal);
                } catch (RuntimeException ignored) {
                    // Instrumentation must not change the application's result.
                }
            }
            for (NativeTelemetry.InvocationSpan nativeSpan : new ArrayList<>(open)) {
                nativeSpan.endAbandoned();
            }
        } finally {
            span.end();
        }
    }
}
