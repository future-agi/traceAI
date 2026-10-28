package ai.traceai.spring;

import ai.traceai.ContextAttributes;
import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import reactor.util.context.ContextView;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.function.UnaryOperator;

/**
 * Explicit operation-context supply for the Spring AI reactive wrappers.
 *
 * <p>TraceAI session/user/metadata/tag attributes live in {@link ContextAttributes} thread-locals and the
 * OpenTelemetry parent span lives in the current {@link Context}. Both are thread-bound, which is unreliable
 * for a {@code Flux} that is assembled on one thread, subscribed on another and signalled from a provider
 * I/O thread. Use this helper to bind the parent context and the TraceAI attributes to the Reactor
 * subscriber context instead:</p>
 *
 * <pre>
 * tracedModel.stream(prompt)
 *     .contextWrite(SpringAITracingContext.withContext(Context.current(), ContextAttributes.getAttributesFromContext()))
 * </pre>
 *
 * <p>Selection order for every span the wrappers create:</p>
 * <ol>
 *   <li>an explicit snapshot supplied with {@link #withContext(Context, Map)} (parent and attributes override
 *       everything else as one unit; {@code Context.root()} plus an empty map is an explicit empty context,
 *       not absence);</li>
 *   <li>otherwise the subscription-time current OpenTelemetry span (when valid) together with the current
 *       {@link ContextAttributes} snapshot;</li>
 *   <li>otherwise, when only subscription-time attributes are present, those attributes with the current
 *       (possibly root) parent;</li>
 *   <li>otherwise the snapshot captured when {@code stream(prompt)} was assembled.</li>
 * </ol>
 *
 * <p>Reusing one publisher across tenants therefore requires an explicit snapshot per subscription.</p>
 */
public final class SpringAITracingContext {

    /** Private namespaced Reactor context key. Callers use the helper, never this key. */
    static final String CONTEXT_KEY = "ai.traceai.spring.operation-context.v1";

    private static final Set<String> ALLOWED_KEYS = Set.of(
        SemanticConventions.SESSION_ID,
        SemanticConventions.GEN_AI_CONVERSATION_ID,
        SemanticConventions.USER_ID,
        SemanticConventions.METADATA,
        SemanticConventions.TAG_TAGS);

    private SpringAITracingContext() {
        throw new UnsupportedOperationException("Utility class");
    }

    /**
     * Builds a Reactor context operator that binds the given OpenTelemetry parent and TraceAI attributes to
     * every span the wrapped model creates for that subscription.
     *
     * @param parent     the OpenTelemetry parent context; use {@link Context#root()} for no parent
     * @param attributes TraceAI attribute strings as produced by {@link ContextAttributes#getAttributesFromContext()}
     *                   (keys {@code session.id}, {@code gen_ai.conversation.id}, {@code user.id}, {@code metadata},
     *                   {@code tag.tags}); the map is copied, metadata/tags stay verbatim serialized strings
     * @return an operator for {@code Flux.contextWrite(...)}
     * @throws NullPointerException     if parent or attributes is null
     * @throws IllegalArgumentException for unknown keys or null values
     */
    public static UnaryOperator<reactor.util.context.Context> withContext(Context parent, Map<String, String> attributes) {
        Objects.requireNonNull(parent, "parent context must not be null");
        Objects.requireNonNull(attributes, "attributes must not be null");
        Map<String, String> copy = new LinkedHashMap<>();
        for (Map.Entry<String, String> entry : attributes.entrySet()) {
            String key = entry.getKey();
            if (key == null || !ALLOWED_KEYS.contains(key)) {
                throw new IllegalArgumentException("Unsupported TraceAI context attribute key: " + key);
            }
            if (entry.getValue() == null) {
                throw new IllegalArgumentException("TraceAI context attribute value must not be null: " + key);
            }
            copy.put(key, entry.getValue());
        }
        Snapshot snapshot = new Snapshot(parent, Collections.unmodifiableMap(copy));
        return reactorContext -> reactorContext.put(CONTEXT_KEY, snapshot);
    }

    /** Captures the current thread's parent context and TraceAI attributes. */
    static Snapshot capture() {
        return new Snapshot(Context.current(), ContextAttributes.getAttributesFromContext());
    }

    /**
     * Selects the operation context for one subscription.
     *
     * @param subscriberContext the downstream Reactor context at subscription time
     * @param assembly          the snapshot captured at assembly time (fallback)
     */
    static Snapshot select(ContextView subscriberContext, Snapshot assembly) {
        Snapshot explicit = subscriberContext.getOrDefault(CONTEXT_KEY, null);
        if (explicit != null) {
            return explicit;
        }
        Context current = Context.current();
        Map<String, String> attributes = ContextAttributes.getAttributesFromContext();
        Span currentSpan = Span.fromContextOrNull(current);
        if (currentSpan != null && currentSpan.getSpanContext().isValid()) {
            return new Snapshot(current, attributes);
        }
        if (!attributes.isEmpty()) {
            return new Snapshot(current, attributes);
        }
        return assembly;
    }

    /**
     * Starts a span from the selected snapshot: thread-local attributes are cleared while the span is created
     * so stale worker-thread values never leak in, then the snapshot attributes are applied verbatim.
     */
    static Span startSpan(FITracer tracer, String name, FISpanKind kind, Snapshot snapshot) {
        AutoCloseable session = ContextAttributes.usingSession(null);
        try {
            AutoCloseable user = ContextAttributes.usingUser(null);
            try {
                AutoCloseable metadata = ContextAttributes.usingMetadata(null);
                try {
                    AutoCloseable tags = ContextAttributes.usingTags(null);
                    try {
                        Span span = tracer.startSpan(name, kind, snapshot.parent());
                        for (Map.Entry<String, String> entry : snapshot.attributes().entrySet()) {
                            span.setAttribute(entry.getKey(), entry.getValue());
                        }
                        return span;
                    } finally {
                        closeQuietly(tags);
                    }
                } finally {
                    closeQuietly(metadata);
                }
            } finally {
                closeQuietly(user);
            }
        } finally {
            closeQuietly(session);
        }
    }

    private static void closeQuietly(AutoCloseable scope) {
        try {
            scope.close();
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            // ContextAttributes scopes only restore thread-locals; a checked exception cannot occur here.
            throw new IllegalStateException("Failed to restore TraceAI context attributes", e);
        }
    }

    /** Immutable selected operation context. */
    static final class Snapshot {
        private final Context parent;
        private final Map<String, String> attributes;

        Snapshot(Context parent, Map<String, String> attributes) {
            this.parent = Objects.requireNonNull(parent, "parent");
            this.attributes = Collections.unmodifiableMap(new LinkedHashMap<>(Objects.requireNonNull(attributes, "attributes")));
        }

        Context parent() {
            return parent;
        }

        Map<String, String> attributes() {
            return attributes;
        }
    }
}
