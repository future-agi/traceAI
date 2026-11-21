package ai.traceai.apptest;

import ai.traceai.annotations.Agent;
import java.lang.reflect.Method;

/**
 * A child-JVM application whose annotated {@code @Agent} method runs a real Google ADK agent (the google-adk module's AdkApp, reached
 * reflectively because ADK is not on the compile class path of this module). No tracing code.
 *
 * <p>Arguments: AdkApp's own (mode, model endpoint, prompt, where the model's HTTP call is sent, scenario).
 */
public final class AnnotatedAdkApp {
    private static volatile String[] adkArguments;

    public static void main(String[] args) throws Exception {
        adkArguments = args;
        System.out.println("RESULT=" + new AnnotatedAdkApp().plan(args[2]));
        Class<?> adk = Class.forName("ai.traceai.googleadk.AdkApp");
        // The annotated span has ended; only now does AdkApp report and shut the agent down.
        adk.getMethod("finish").invoke(null);
    }

    @Agent(name = "planner")
    public String plan(String question) throws Exception {
        Method runAdk = Class.forName("ai.traceai.googleadk.AdkApp").getMethod("runDeferred", String[].class);
        runAdk.invoke(null, (Object)adkArguments);
        return "planned";
    }
}
