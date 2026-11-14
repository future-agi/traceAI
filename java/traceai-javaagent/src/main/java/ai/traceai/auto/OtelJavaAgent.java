package ai.traceai.auto;

/**
 * Detects the OpenTelemetry Java agent in this JVM. Both agents rewrite the JDK HttpClient and OkHttp,
 * and that agent replaces the application's spans with its own bridged spans, which this agent's
 * deduplication under library spans cannot read. Running both is not known to be safe, so this
 * agent stays off beside it. When that agent is listed after this one, it has not started yet when
 * this agent starts, so TraceAIAuto looks again before the first capture. Names are built at runtime
 * so Shade does not relocate them.
 */
final class OtelJavaAgent {
    /** Class-name prefix of that agent's own classes: bootstrap, tooling and premain. */
    static final String PREFIX = String.join(".", "io", "opentelemetry", "javaagent", "");
    // Its premain class, found when that agent's jar is visible on the system class path.
    private static final String PREMAIN = String.join("/", "io", "opentelemetry", "javaagent", "OpenTelemetryAgent.class");
    // On the bootstrap class path once that agent has started, including after a runtime attach.
    private static final String BOOTSTRAP = PREFIX + "bootstrap.AgentInitializer";
    static boolean present() {
        try { if (ClassLoader.getSystemClassLoader().getResource(PREMAIN) != null) return true; } catch (Throwable ignored) { }
        try { Class.forName(BOOTSTRAP, false, null); return true; } catch (Throwable ignored) { return false; }
    }
    private OtelJavaAgent() {}
}
