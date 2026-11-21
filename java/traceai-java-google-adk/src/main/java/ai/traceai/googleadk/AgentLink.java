package ai.traceai.googleadk;

import java.lang.reflect.Method;

/**
 * The wrappers' side of the optional FI_GOOGLE_ADK_SKIP_WRAPPED_CALLS setting of the traceAI javaagent: when the agent is attached, a
 * {@link TracedTool} tells it about each call it receives, so the agent can leave that call to the wrapper's span. Without the agent there is
 * nothing to tell and nothing is recorded here.
 */
final class AgentLink {
    private static final Method WRAPPED_TOOL = lookup();

    private AgentLink() {}

    private static Method lookup() {
        try {
            // The agent's classes are on the system class path, whichever class loader the application uses.
            return Class.forName("ai.traceai.auto.GoogleAdk", false, ClassLoader.getSystemClassLoader())
                .getMethod("wrappedTool", Object.class, Object.class);
        } catch (Throwable absent) {
            return null;
        }
    }

    /** Tells the agent, if there is one, that {@code tool} is about to run with this arguments map. Never fails the call. */
    static void wrappedTool(Object arguments, Object tool) {
        if (WRAPPED_TOOL == null || arguments == null) return;
        try {
            WRAPPED_TOOL.invoke(null, arguments, tool);
        } catch (Throwable ignored) {
            // The call is recorded by both, as without the option.
        }
    }
}
