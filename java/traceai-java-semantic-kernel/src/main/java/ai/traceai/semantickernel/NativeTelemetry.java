package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.SemanticConventions;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.contextvariables.ContextVariableType;
import com.microsoft.semantickernel.contextvariables.ContextVariableTypes;
import com.microsoft.semantickernel.hooks.KernelHook.PreToolCallHook;
import com.microsoft.semantickernel.hooks.KernelHooks;
import com.microsoft.semantickernel.hooks.PreToolCallEvent;
import com.microsoft.semantickernel.implementation.telemetry.SemanticKernelTelemetry;
import com.microsoft.semantickernel.orchestration.FunctionInvocation;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import com.microsoft.semantickernel.orchestration.InvocationReturnMode;
import com.microsoft.semantickernel.plugin.KernelPlugin;
import com.microsoft.semantickernel.services.AIService;
import com.microsoft.semantickernel.services.AIServiceCollection;
import com.microsoft.semantickernel.services.AIServiceSelector;
import com.microsoft.semantickernel.services.ServiceNotFoundException;
import com.microsoft.semantickernel.semanticfunctions.KernelArguments;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
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
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.annotation.Nullable;
import reactor.core.publisher.Mono;

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
    private static final String FUNCTION_NAME = "semantic_kernel.function.invocation.name";
    private static final String FUNCTION_PLUGIN = "semantic_kernel.function.invocation.plugin_name";

    private NativeTelemetry() { }

    static SemanticKernelTelemetry create(TracedInvocation invocation) {
        return new SemanticKernelTelemetry(new InvocationTracer(invocation), null);
    }

    /**
     * Uses this invocation's telemetry, including for functions the model calls automatically,
     * without changing any other invocation setting.
     */
    static InvocationContext route(@Nullable InvocationContext context, TracedInvocation invocation) {
        return new RoutedInvocationContext(context, invocation);
    }

    /**
     * A view of {@code kernel} whose functions report through this invocation. Semantic Kernel's
     * prompt template engines ({@code {{plugin.function}}} and Handlebars helpers) and native
     * functions that take a {@code Kernel} call other functions through the kernel they were given,
     * without the caller's invocation context.
     */
    @Nullable
    static Kernel routeKernel(@Nullable Kernel kernel, TracedInvocation invocation) {
        if (kernel == null) {
            return null;
        }
        if (kernel instanceof RoutedKernel) {
            RoutedKernel routed = (RoutedKernel) kernel;
            if (routed.invocation == invocation) {
                return routed;
            }
            kernel = routed.delegate;
        }
        return new RoutedKernel(kernel, invocation);
    }

    private static boolean isCompletion(String name) {
        return name.startsWith("chat.completions") || name.startsWith("text.completions");
    }

    /**
     * Sets this invocation's telemetry and tool hook as real fields, so a context Semantic Kernel
     * copies keeps them.
     */
    private static final class RoutedInvocationContext extends InvocationContext {
        RoutedInvocationContext(@Nullable InvocationContext source, TracedInvocation invocation) {
            super(
                KernelHooks.merge(source == null ? null : source.getKernelHooks(), toolHooks(invocation)),
                source == null ? null : source.getPromptExecutionSettings(),
                source == null ? null : source.getToolCallBehavior(),
                source == null ? null : source.getFunctionChoiceBehavior(),
                source == null ? null : source.getContextVariableTypes(),
                source == null ? InvocationReturnMode.NEW_MESSAGES_ONLY : source.returnMode(),
                invocation.telemetry());
        }
    }

    private static final String TOOL_HOOK = "traceai.semantic-kernel.tool-telemetry";

    /**
     * Semantic Kernel's OpenAI connector runs automatic tool calls without forwarding the caller's
     * telemetry, so their function spans would otherwise go to the global OpenTelemetry tracer and
     * skip traceAI's span kinds and privacy settings. The connector lets a pre-tool-call hook
     * replace the function it is about to run.
     */
    private static KernelHooks toolHooks(TracedInvocation invocation) {
        KernelHooks hooks = new KernelHooks();
        hooks.addHook(TOOL_HOOK, new RouteToolCall(invocation));
        return hooks;
    }

    private static final class RouteToolCall implements PreToolCallHook {
        private final TracedInvocation invocation;

        RouteToolCall(TracedInvocation invocation) {
            this.invocation = invocation;
        }

        @Override
        public PreToolCallEvent apply(PreToolCallEvent event) {
            KernelFunction<?> function = event.getFunction();
            if (function == null || (function instanceof RoutedFunction
                    && ((RoutedFunction<?>) function).invocation == invocation)) {
                return event;
            }
            // The connector reads only the function and arguments from the returned event.
            return new PreToolCallEvent(function.getName(), event.getArguments(),
                RoutedFunction.wrap(function, invocation), new ContextVariableTypes());
        }

        @Override
        public int getPriority() {
            // Run after the caller's hooks, so the function they choose is the one traced.
            return Integer.MAX_VALUE;
        }
    }

    /** Runs a function with this invocation's telemetry; otherwise identical to it. */
    private static final class RoutedFunction<T> extends KernelFunction<T> {
        private final KernelFunction<T> delegate;
        private final TracedInvocation invocation;

        private RoutedFunction(KernelFunction<T> delegate, TracedInvocation invocation) {
            super(delegate.getMetadata(), delegate.getExecutionSettings());
            this.delegate = delegate;
            this.invocation = invocation;
        }

        static <T> KernelFunction<T> wrap(KernelFunction<T> function, TracedInvocation invocation) {
            if (function instanceof RoutedFunction) {
                RoutedFunction<T> routed = (RoutedFunction<T>) function;
                if (routed.invocation == invocation) {
                    return routed;
                }
                // The innermost traced operation owns the call.
                function = routed.delegate;
            }
            return new RoutedFunction<>(function, invocation);
        }

        @Override
        public Mono<FunctionResult<T>> invokeAsync(Kernel kernel, @Nullable KernelArguments arguments,
                @Nullable ContextVariableType<T> variableType, @Nullable InvocationContext invocationContext) {
            return delegate.invokeAsync(routeKernel(kernel, invocation), arguments, variableType,
                route(invocationContext, invocation));
        }
    }

    /**
     * Delegates to the application's kernel and wraps every function it hands out. The copied
     * plugins and hooks passed to the superclass only serve {@code Kernel.from(...)} and
     * {@code toBuilder()}; every lookup reads the live delegate.
     */
    private static final class RoutedKernel extends Kernel {
        private final Kernel delegate;
        private final TracedInvocation invocation;

        RoutedKernel(Kernel delegate, TracedInvocation invocation) {
            this(delegate, invocation, copyServices(delegate));
        }

        private RoutedKernel(Kernel delegate, TracedInvocation invocation, AIServiceCollection services) {
            // Kernel.from(view) reads the superclass's private fields, not our getters. Supply a
            // real service snapshot and a provider that rebuilds the original selector against
            // the copy's services. A constant selector would silently ignore withAIService(...).
            super(services, copied -> copied == services ? delegate.getServiceSelector()
                    : copySelector(delegate, copied),
                new ArrayList<>(delegate.getPlugins()), delegate.getGlobalKernelHooks());
            this.delegate = delegate;
            this.invocation = invocation;
        }

        private static AIServiceCollection copyServices(Kernel delegate) {
            AIServiceCollection snapshot = new AIServiceCollection();
            // The builder exposes its copied collection to a selector provider. Use that public
            // API rather than reflecting into Kernel's private fields. Do not invoke the user's
            // selector factory again merely to create the tracing view.
            delegate.toBuilder().withServiceSelector(services -> {
                snapshot.putAll(services);
                return delegate.getServiceSelector();
            }).build();
            return snapshot;
        }

        private static AIServiceSelector copySelector(Kernel delegate, AIServiceCollection services) {
            Kernel.Builder builder = delegate.toBuilder();
            services.forEach((type, service) -> copyService(builder, type, service));
            return builder.build().getServiceSelector();
        }

        private static <S extends AIService> void copyService(Kernel.Builder builder,
                Class<S> type, AIService service) {
            builder.withAIService(type, type.cast(service));
        }

        @Override
        public <T> FunctionInvocation<T> invokeAsync(KernelFunction<T> function) {
            return RoutedFunction.wrap(function, invocation).invokeAsync(this);
        }

        @Override
        public <T> FunctionInvocation<T> invokePromptAsync(String prompt, KernelArguments arguments) {
            return invokeAsync(KernelFunction.<T>createFromPrompt(prompt).build()).withArguments(arguments);
        }

        @Override
        public <T> FunctionInvocation<T> invokePromptAsync(String prompt, KernelArguments arguments,
                InvocationContext context) {
            return this.<T>invokePromptAsync(prompt, arguments).withInvocationContext(context);
        }

        @Override
        @Nullable
        public KernelPlugin getPlugin(String pluginName) {
            KernelPlugin plugin = delegate.getPlugin(pluginName);
            return plugin == null ? null : routePlugin(plugin);
        }

        @Override
        public Collection<KernelPlugin> getPlugins() {
            List<KernelPlugin> plugins = new ArrayList<>();
            for (KernelPlugin plugin : delegate.getPlugins()) {
                plugins.add(routePlugin(plugin));
            }
            return Collections.unmodifiableList(plugins);
        }

        @Override
        public <U> KernelFunction<U> getFunction(String pluginName, String functionName) {
            return RoutedFunction.wrap(delegate.<U>getFunction(pluginName, functionName), invocation);
        }

        @Override
        public List<KernelFunction<?>> getFunctions() {
            List<KernelFunction<?>> functions = new ArrayList<>();
            for (KernelFunction<?> function : delegate.getFunctions()) {
                functions.add(RoutedFunction.wrap(function, invocation));
            }
            return functions;
        }

        @Override
        public KernelHooks getGlobalKernelHooks() {
            return delegate.getGlobalKernelHooks();
        }

        @Override
        public AIServiceSelector getServiceSelector() {
            return delegate.getServiceSelector();
        }

        @Override
        public <S extends AIService> S getService(Class<S> clazz) throws ServiceNotFoundException {
            return delegate.getService(clazz);
        }

        @Override
        public <S extends AIService> S getService(Class<S> clazz, KernelArguments args)
                throws ServiceNotFoundException {
            return delegate.getService(clazz, args);
        }

        @Override
        public Kernel.Builder toBuilder() {
            return delegate.toBuilder();
        }

        private KernelPlugin routePlugin(KernelPlugin plugin) {
            Map<String, KernelFunction<?>> functions = new LinkedHashMap<>();
            for (Map.Entry<String, KernelFunction<?>> entry : plugin.getFunctions().entrySet()) {
                functions.put(entry.getKey(), RoutedFunction.wrap(entry.getValue(), invocation));
            }
            return new KernelPlugin(plugin.getName(), plugin.getDescription(), functions);
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
        @Nullable private String functionName;
        @Nullable private String pluginName;

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
            if (value != null) {
                switch (key.getKey()) {
                    case "gen_ai.system":
                        provider = value.toString();
                        break;
                    case FUNCTION_NAME:
                        functionName = value.toString();
                        break;
                    case FUNCTION_PLUGIN:
                        pluginName = value.toString();
                        break;
                    default:
                        break;
                }
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
            if (kind == FISpanKind.TOOL && functionName != null) {
                // The name the model sees for the function, as in the span name.
                span.setAttribute(SemanticConventions.GEN_AI_TOOL_NAME,
                    pluginName == null || pluginName.isEmpty() ? functionName : pluginName + "-" + functionName);
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
