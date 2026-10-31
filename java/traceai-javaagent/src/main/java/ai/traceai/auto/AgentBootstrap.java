package ai.traceai.auto;

import java.lang.instrument.Instrumentation;

/** Java 8 ABI only. Never link implementation classes until the runtime gate succeeds. */
public final class AgentBootstrap {
    public static void premain(final String args, final Instrumentation instrumentation) {
        launchAgent("premain", args, instrumentation);
    }
    public static void agentmain(final String args, final Instrumentation instrumentation) {
        launchAgent("agentmain", args, instrumentation);
    }
    public static void main(final String[] args) {
        runIfSupported(runtimeVersion(), new Runnable() {
            @Override public void run() {
                try {
                    Class.forName("ai.traceai.auto.TraceAIAuto", true, AgentBootstrap.class.getClassLoader())
                        .getMethod("main", String[].class).invoke(null, (Object) args);
                } catch (Throwable ignored) { failed(); }
            }
        });
    }
    private static void launchAgent(final String method, final String args, final Instrumentation instrumentation) {
        runIfSupported(runtimeVersion(), new Runnable() {
            @Override public void run() {
                try {
                    Class.forName("ai.traceai.auto.TraceAIAgent", true, AgentBootstrap.class.getClassLoader())
                        .getMethod(method, String.class, Instrumentation.class).invoke(null, args, instrumentation);
                } catch (Throwable ignored) { failed(); }
            }
        });
    }
    private static String runtimeVersion() {
        try { return System.getProperty("java.specification.version"); }
        catch (Throwable ignored) { return null; }
    }
    static void runIfSupported(String version, Runnable implementation) {
        try {
            String major = version != null && version.startsWith("1.") ? version.substring(2) : version;
            if (major == null || Integer.parseInt(major) < 17) {
                unsupported(); return;
            }
        } catch (Throwable ignored) { unsupported(); return; }
        try { implementation.run(); } catch (Throwable ignored) { failed(); }
    }
    private static void unsupported() {
        try { System.err.println("[traceai-auto] Java 17+ required; application runs without auto-instrumentation"); }
        catch (Throwable ignored) { }
    }
    private static void failed() {
        try { System.err.println("[traceai-auto] Agent startup failed; application runs without auto-instrumentation"); }
        catch (Throwable ignored) { }
    }
    private AgentBootstrap() { }
}
