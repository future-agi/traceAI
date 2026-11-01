package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.SemanticConventions;
import com.microsoft.semantickernel.implementation.telemetry.SemanticKernelTelemetry;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanBuilder;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.annotation.Nullable;

/**
 * Routes Semantic Kernel's own spans for one traced invocation through the traceAI tracer.
 *
 * <p>Semantic Kernel starts its function and chat spans from the thread-local context and closes
 * their scopes in an order that leaves the starting thread's context pointing at the ended span.
 * This adapter keeps those spans in the invocation's trace, removes the duplicate producer when a
 * traceAI wrapper already owns the operation, applies traceAI span kinds and privacy settings, and
 * lets the owning invocation end spans that cancellation would otherwise leave open.</p>
 */
final class NativeTelemetry {

    private static final String FUNCTION_PREFIX = "function_invocation";
    private static final AttributeKey<Long> INPUT_TOKENS =
        AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
    private static final AttributeKey<Long> OUTPUT_TOKENS =
        AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
    // Semantic Kernel 1.4.x used these names before moving to gen_ai.usage.*.
    private static final String LEGACY_INPUT_TOKENS = "gen_ai.response.prompt_tokens";
    private static final String LEGACY_OUTPUT_TOKENS = "gen_ai.response.completion_tokens";

    private NativeTelemetry() { }

    static SemanticKernelTelemetry create(TracedInvocation invocation) {
        return new SemanticKernelTelemetry(new InvocationTracer(invocation), null);
    }

    /** Uses this invocation's telemetry without changing any other invocation setting. */
    static InvocationContext route(@Nullable InvocationContext context, TracedInvocation invocation) {
        return new RoutedInvocationContext(context, invocation.telemetry());
    }

    private static boolean isCompletion(String name) {
        return name.startsWith("chat.completions") || name.startsWith("text.completions");
    }

    private static final class RoutedInvocationContext extends InvocationContext {
        private final SemanticKernelTelemetry telemetry;

        RoutedInvocationContext(@Nullable InvocationContext source, SemanticKernelTelemetry telemetry) {
            super(source);
            this.telemetry = telemetry;
        }

        @Override
        public SemanticKernelTelemetry getTelemetry() {
            return telemetry;
        }
    }

    private static final class InvocationTracer implements Tracer {
        private final TracedInvocation invocation;

        InvocationTracer(TracedInvocation invocation) {
            this.invocation = invocation;
        }

        @Override
        public SpanBuilder spanBuilder(String spanName) {
            return new InvocationSpanBuilder(invocation, spanName);
        }
    }

    private static final class InvocationSpanBuilder implements SpanBuilder {
        private final TracedInvocation invocation;
        private final String name;
        private final SpanBuilder delegate;
        @Nullable private Context parent;
        private boolean noParent;
        @Nullable private String provider;

        InvocationSpanBuilder(TracedInvocation invocation, String name) {
            this.invocation = invocation;
            this.name = name;
            this.delegate = invocation.tracer().getTracer().spanBuilder(name);
        }

        @Override
        public SpanBuilder setParent(Context context) {
            parent = context;
            noParent = false;
            return this;
        }

        @Override
        public SpanBuilder setNoParent() {
            parent = null;
            noParent = true;
            return this;
        }

        @Override
        public SpanBuilder addLink(SpanContext spanContext) {
            delegate.addLink(spanContext);
            return this;
        }

        @Override
        public SpanBuilder addLink(SpanContext spanContext, Attributes attributes) {
            delegate.addLink(spanContext, attributes);
            return this;
        }

        @Override
        public SpanBuilder setAttribute(String key, String value) {
            return setAttribute(AttributeKey.stringKey(key), value);
        }

        @Override
        public SpanBuilder setAttribute(String key, long value) {
            return setAttribute(AttributeKey.longKey(key), value);
        }

        @Override
        public SpanBuilder setAttribute(String key, double value) {
            return setAttribute(AttributeKey.doubleKey(key), value);
        }

        @Override
        public SpanBuilder setAttribute(String key, boolean value) {
            return setAttribute(AttributeKey.booleanKey(key), value);
        }

        @Override
        public <T> SpanBuilder setAttribute(AttributeKey<T> key, T value) {
            if ("gen_ai.system".equals(key.getKey()) && value != null) {
                provider = value.toString();
            }
            delegate.setAttribute(key, value);
            return this;
        }

        @Override
        public SpanBuilder setSpanKind(SpanKind spanKind) {
            delegate.setSpanKind(spanKind);
            return this;
        }

        @Override
        public SpanBuilder setStartTimestamp(long startTimestamp, TimeUnit unit) {
            delegate.setStartTimestamp(startTimestamp, unit);
            return this;
        }

        @Override
        public Span startSpan() {
            Context resolved = resolveParent();
            boolean function = name.startsWith(FUNCTION_PREFIX);
            boolean completion = isCompletion(name);

            if (function && invocation.consumeRootFunction()) {
                // The traceAI wrapper span already represents this function call. Code running in
                // the function still sees that span as current and can add attributes to it.
                return new InvocationSpan(invocation.span(), invocation, Mode.ALIAS);
            }
            if (completion) {
                invocation.nativeModelStarted();
            }

            if (noParent) {
                delegate.setNoParent();
            } else {
                delegate.setParent(resolved);
            }
            Span span = delegate.startSpan();
            FISpanKind kind = completion ? FISpanKind.LLM : function ? FISpanKind.TOOL : null;
            if (kind != null) {
                span.setAttribute(SemanticConventions.FI_SPAN_KIND, kind.getValue());
                span.setAttribute(SemanticConventions.GEN_AI_SPAN_KIND, kind.getValue().toLowerCase());
            }
            if (provider != null) {
                span.setAttribute(SemanticConventions.LLM_SYSTEM, provider);
            }
            for (Map.Entry<String, String> entry : invocation.contextAttributes().entrySet()) {
                span.setAttribute(entry.getKey(), entry.getValue());
            }
            return invocation.track(new InvocationSpan(span, invocation, Mode.RECORDING));
        }

        private Context resolveParent() {
            if (noParent) {
                return Context.root();
            }
            Context candidate = parent != null ? parent : Context.current();
            SpanContext candidateSpan = Span.fromContext(candidate).getSpanContext();
            // Semantic Kernel reads the thread-local context, which is unrelated after an async hop.
            if (candidateSpan.isValid() && candidateSpan.getTraceId().equals(invocation.traceId())) {
                return candidate;
            }
            return invocation.context();
        }
    }

    private enum Mode {
        /** A Semantic Kernel span exported through traceAI. */
        RECORDING,
        /** Stands in for the traceAI wrapper span, which owns its own status and end. */
        ALIAS
    }

    /** A Semantic Kernel span with traceAI privacy, token totals and cancellation cleanup. */
    static final class InvocationSpan implements Span {
        private final Span delegate;
        private final TracedInvocation invocation;
        private final Mode mode;
        private final AtomicBoolean ended = new AtomicBoolean();
        private final AtomicBoolean firstScope = new AtomicBoolean(true);
        private volatile Long inputTokens;
        private volatile Long outputTokens;
        private volatile boolean hasTotal;

        private InvocationSpan(Span delegate, TracedInvocation invocation, Mode mode) {
            this.delegate = delegate;
            this.invocation = invocation;
            this.mode = mode;
        }

        @Override
        @SuppressWarnings("unchecked")
        public <T> Span setAttribute(AttributeKey<T> key, T value) {
            String name = key.getKey();
            if (value instanceof Number && (LEGACY_INPUT_TOKENS.equals(name) || INPUT_TOKENS.getKey().equals(name))) {
                inputTokens = ((Number) value).longValue();
                delegate.setAttribute(INPUT_TOKENS, inputTokens);
                return this;
            }
            if (value instanceof Number && (LEGACY_OUTPUT_TOKENS.equals(name) || OUTPUT_TOKENS.getKey().equals(name))) {
                outputTokens = ((Number) value).longValue();
                delegate.setAttribute(OUTPUT_TOKENS, outputTokens);
                return this;
            }
            if (SemanticConventions.LLM_TOKEN_COUNT_TOTAL.equals(name)) {
                hasTotal = true;
            }
            delegate.setAttribute(key, value);
            return this;
        }

        @Override
        public Span addEvent(String name, Attributes attributes) {
            delegate.addEvent(name, attributes);
            return this;
        }

        @Override
        public Span addEvent(String name, Attributes attributes, long timestamp, TimeUnit unit) {
            delegate.addEvent(name, attributes, timestamp, unit);
            return this;
        }

        @Override
        public Span setStatus(StatusCode statusCode, String description) {
            if (mode == Mode.ALIAS) {
                return this;
            }
            if (statusCode == StatusCode.ERROR && invocation.tracer().getConfig().isHideErrorDetails()) {
                delegate.setStatus(StatusCode.ERROR);
            } else {
                delegate.setStatus(statusCode, description);
            }
            return this;
        }

        @Override
        public Span recordException(Throwable exception, Attributes additionalAttributes) {
            if (mode == Mode.ALIAS) {
                return this;
            }
            delegate.setAttribute(SemanticConventions.ERROR_TYPE, exception.getClass().getName());
            if (invocation.tracer().getConfig().isHideErrorDetails()) {
                delegate.addEvent("exception", Attributes.of(
                    AttributeKey.stringKey("exception.type"), exception.getClass().getName()));
            } else {
                delegate.recordException(exception, additionalAttributes);
            }
            return this;
        }

        @Override
        public Span updateName(String name) {
            if (mode != Mode.ALIAS) {
                delegate.updateName(name);
            }
            return this;
        }

        @Override
        public void end() {
            if (finish() && mode != Mode.ALIAS) {
                delegate.end();
            }
        }

        @Override
        public void end(long timestamp, TimeUnit unit) {
            if (finish() && mode != Mode.ALIAS) {
                delegate.end(timestamp, unit);
            }
        }

        /** Ends a span that Semantic Kernel left open when the traced operation was cancelled. */
        void endAbandoned() {
            if (delegate.isRecording()) {
                delegate.setAttribute(ReactiveTracing.CANCELLED, true);
            }
            end();
        }

        private boolean finish() {
            if (!ended.compareAndSet(false, true)) {
                return false;
            }
            if (mode == Mode.RECORDING) {
                if (!hasTotal && inputTokens != null && outputTokens != null) {
                    delegate.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, inputTokens + outputTokens);
                }
                invocation.untrack(this);
            }
            return true;
        }

        @Override
        public SpanContext getSpanContext() {
            return delegate.getSpanContext();
        }

        @Override
        public boolean isRecording() {
            return mode == Mode.ALIAS ? delegate.isRecording() : !ended.get() && delegate.isRecording();
        }

        @Override
        public Scope makeCurrent() {
            // Semantic Kernel first makes a context containing this span current, then makes the span
            // current again and closes the two scopes in the opposite order. Skipping that redundant
            // inner scope lets the outer scope restore the caller's context.
            if (firstScope.compareAndSet(true, false) && Span.fromContext(Context.current()) == this) {
                return Scope.noop();
            }
            return Context.current().with(this).makeCurrent();
        }
    }
}
