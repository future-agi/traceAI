package ai.traceai.semantickernel;

import io.opentelemetry.context.Context;
import io.opentelemetry.instrumentation.reactor.v3_1.ContextPropagationOperator;
import reactor.util.context.ContextView;

/**
 * Carries the OpenTelemetry context in the Reactor context.
 *
 * <p>Semantic Kernel 1.4.5 and later bring the maintained OpenTelemetry Reactor bridge
 * ({@code opentelemetry-reactor-3.1}); when it is on the classpath it is registered and its context
 * key is used. Earlier releases such as 1.3.0 do not bring it, and this artifact does not add it to
 * the application's dependency graph. There, the adapter's own spans still nest through a private
 * Reactor context key, but the current span is not propagated across scheduler hops for
 * application code.</p>
 */
final class ReactorOtelContext {

    private static final String BRIDGE_CLASS =
        "io.opentelemetry.instrumentation.reactor.v3_1.ContextPropagationOperator";
    private static final Object FALLBACK_KEY = new Object();
    private static final boolean BRIDGE = installBridge();

    private ReactorOtelContext() { }

    /** Whether the maintained Reactor bridge is present and registered. */
    static boolean bridged() {
        return BRIDGE;
    }

    static reactor.util.context.Context store(reactor.util.context.Context reactorContext, Context context) {
        return BRIDGE ? Bridge.store(reactorContext, context) : reactorContext.put(FALLBACK_KEY, context);
    }

    static Context read(ContextView view, Context fallback) {
        if (BRIDGE) {
            return Bridge.read(view, fallback);
        }
        Object stored = view.getOrDefault(FALLBACK_KEY, null);
        return stored instanceof Context ? (Context) stored : fallback;
    }

    private static boolean installBridge() {
        try {
            Class.forName(BRIDGE_CLASS, false, ReactorOtelContext.class.getClassLoader());
        } catch (ClassNotFoundException | LinkageError absent) {
            return false;
        }
        try {
            Bridge.register();
            return true;
        } catch (LinkageError incompatible) {
            return false;
        }
    }

    /** Loaded only after the bridge class has been found. */
    private static final class Bridge {
        static void register() {
            // Registration is idempotent and does not install an exporter or change global OTel.
            ContextPropagationOperator.create().registerOnEachOperator();
        }

        static reactor.util.context.Context store(reactor.util.context.Context reactorContext, Context context) {
            return ContextPropagationOperator.storeOpenTelemetryContext(reactorContext, context);
        }

        static Context read(ContextView view, Context fallback) {
            return ContextPropagationOperator.getOpenTelemetryContextFromContextView(view, fallback);
        }
    }
}
