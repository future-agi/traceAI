package ai.traceai.auto;

import java.util.Map;

/** Starts with -javaagent and no keys in the environment; the keys arrive later through init(options). */
public final class LateKeysApp {
    static void run(String[] args) throws Exception {
        Class<?> agent = Class.forName("ai.traceai.auto.TraceAIAuto");
        Object provider = agent.getMethod("init", Map.class).invoke(null, Map.of("api_key", "test-api-key", "secret_key", "test-secret-key"));
        if (provider == null) throw new AssertionError("init with keys after a keyless start did not start");
        if (provider != agent.getMethod("init").invoke(null)) throw new AssertionError("a later init replaced the running provider");
        PlainApp.call(args);
        agent.getMethod("flush").invoke(null);
    }
}
