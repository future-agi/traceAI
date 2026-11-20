package ai.traceai.apptest;

import ai.traceai.annotations.*;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * A child-JVM application with annotated methods and no tracing code. Scenarios: sync, async, reactor, throwing,
 * plain (nothing annotated is traced when the agent is off). The package is outside ai.traceai.auto on purpose:
 * the agent never instruments its own package.
 */
public final class AnnotatedApp {
    public static final String SECRET_ARG = "very-secret-argument-value";

    public static void main(String[] args) throws Exception {
        String scenario = args[0];
        if (scenario.equals("init_first")) { Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("init").invoke(null); scenario = "sync"; }
        if (scenario.equals("init_twice")) {
            Class<?> auto = Class.forName("ai.traceai.auto.TraceAIAuto");
            auto.getMethod("init").invoke(null); auto.getMethod("init").invoke(null); scenario = "sync";
        }
        Service service = new Service();
        switch (scenario) {
            case "sync" -> {
                System.out.println("RESULT=" + service.agent("what is 2+2?", SECRET_ARG));
                service.noArguments(); System.out.println("PRIMITIVE=" + service.count(41));
            }
            case "secret" -> System.out.println("RESULT=" + service.agent(System.getenv("FI_API_KEY"), "s"));
            case "error" -> {
                try { service.boom("x"); System.out.println("NO_EXCEPTION"); }
                catch (IllegalStateException expected) { System.out.println("CAUGHT=" + expected.getMessage() + ";same=" + (expected == Service.LAST)); }
            }
            case "async" -> System.out.println("RESULT=" + service.later("q").get(10, TimeUnit.SECONDS));
            case "async_error" -> {
                try { service.laterFails().get(10, TimeUnit.SECONDS); System.out.println("NO_EXCEPTION"); }
                catch (java.util.concurrent.ExecutionException expected) { System.out.println("CAUGHT=" + expected.getCause().getMessage()); }
            }
            case "mono" -> System.out.println("RESULT=" + service.mono("m").block(java.time.Duration.ofSeconds(10)));
            case "flux" -> System.out.println("RESULT=" + service.flux(3).collectList().block(java.time.Duration.ofSeconds(10)));
            case "late_class_load" -> {
                // The annotated class is loaded and used before initialization; a later init() still traces it.
                service.count(1);
                Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("init").invoke(null);
                System.out.println("RESULT=" + service.agent("late", "s"));
            }
            default -> throw new IllegalArgumentException(scenario);
        }
        Class<?> auto = null;
        try { auto = Class.forName("ai.traceai.auto.TraceAIAuto"); } catch (ClassNotFoundException ignored) { }
        if (auto != null) auto.getMethod("flush").invoke(null);
        System.out.println("DONE");
    }

    public static class Service {
        static volatile RuntimeException LAST;

        @Agent(name = "planner", description = "plans the work")
        public String agent(String question, @ExcludeFromSpan String apiKey) {
            String found = lookup(question);
            String answer = generate("Answer: " + found);
            return fetch(List.of(question)) + "|" + answer + "|" + apiKey.length();
        }

        @Tool(name = "calculator", description = "adds numbers")
        public String lookup(String expression) { return "4"; }

        @LLM
        public String generate(String prompt) { return "The answer is 4"; }

        @Span(kind = "retriever", name = "fetch_docs")
        public String fetch(List<String> terms) { return "docs" + terms.size(); }

        @Chain
        public void noArguments() { }

        @Chain
        public int count(int n) { return n + 1; }

        @Chain
        public void boom(String why) {
            LAST = new IllegalStateException("bad " + why + " " + SECRET_ARG);
            throw LAST;
        }

        @Chain
        public CompletableFuture<String> later(String q) {
            return CompletableFuture.supplyAsync(() -> {
                try { Thread.sleep(300); } catch (InterruptedException ignored) { }
                return "later:" + q;
            });
        }

        @Chain
        public CompletableFuture<String> laterFails() {
            return CompletableFuture.supplyAsync(() -> { throw new IllegalArgumentException("async bad"); });
        }

        @Chain
        public Mono<String> mono(String q) { return Mono.delay(java.time.Duration.ofMillis(200)).map(x -> "mono:" + q); }

        @Chain
        public Flux<Integer> flux(int n) { return Flux.range(1, n); }
    }
    private AnnotatedApp() { }
}
