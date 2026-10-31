package ai.traceai.auto;

public final class OneLineApp {
    public static void main(String[] args) throws Exception {
        TraceAIAuto.init();
        JdkOnlyApp.main(args);
    }
}
