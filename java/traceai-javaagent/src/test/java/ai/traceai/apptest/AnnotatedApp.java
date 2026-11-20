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
        if (scenario.equals("init_enabled")) {
            // A copy of the engine whose own settings switch annotations on, whatever another copy in the JVM was told.
            Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("init", java.util.Map.class).invoke(null, java.util.Map.of("FI_DISABLED_INSTRUMENTATIONS", ""));
            scenario = "sync";
        }
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
            case "wire" -> System.out.println("RESULT=" + service.callModel(args[1]));
            case "blank_kind" -> System.out.println("RESULT=" + service.blank() + service.both());
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
            case "credential_text" -> System.out.println("RESULT=" + service.audit(new Account(), new KeyHolder(), new Csv(), new Wrapped(), new Quoted()) + service.audit(new BasicHolder(), new CharsHolder(), new ParenHolder(), new QuoteHolder(), new FlagHolder()));
            case "straddle" -> {
                String secret = System.getenv("FI_API_KEY");
                String jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTWU5USEVUSUMifQ.SYNTHSIGNATUREAAAAAAAAAAAAAA";
                String payload = secret + "-b-" + secret + " " + jwt + " sk-SYNTHKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA Bearer SYNTHBEARERTOKENAAAAAAAAAAAAAAAA tail tail tail";
                int count = 0;
                for (int k = 0; k <= payload.length() + 6; k++) { service.sink("a".repeat(Math.max(1, 196 - k)) + payload); count++; }
                System.out.println("RESULT=" + count);
            }
            case "credential_strings" -> {
                for (String text : credentialStrings()) { service.sink(text); service.echo(text); }
                System.out.println("RESULT=" + credentialStrings().size());
            }
            case "window_edge" -> {
                String secret = System.getenv("FI_API_KEY");
                String jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTWU5USEVUSUMifQ.SYNTHSIGNATUREAAAAAAAAAAAAAA";
                String key = "sk-SYNTHKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
                int count = 0;
                // Three secrets of 4096 characters shrink to three placeholders, so the replaced window is far shorter than the read window.
                for (String shape : List.of(key, jwt)) {
                    for (int edge = Integer.parseInt(args[1]) - 60; edge <= Integer.parseInt(args[1]) + 6; edge++) {
                        String head = secret + " " + secret + " " + secret + " ";
                        service.sink(head + "a".repeat(Math.max(1, edge - head.length())) + shape + " tail tail tail"); count++;
                    }
                }
                System.out.println("RESULT=" + count);
            }
            case "record_credentials" -> System.out.println("RESULT=" + service.signIn(new Service.Login("bob", "SYNTHPW1", "SYNTHPW2", "SYNTHPW3", "SYNTHPW4", "SYNTHPW5", "keep-me")));
            case "publisher_object" -> {
                Object returned = service.asObject(); Object same = Service.PUBLISHED;
                System.out.println("RESULT=object-same=" + (returned == same) + ";type=" + returned.getClass().getName());
                org.reactivestreams.Publisher<String> publisher = service.asPublisher();
                System.out.println("RESULT=publisher-same=" + (publisher == Service.PUBLISHED) + ";type=" + publisher.getClass().getName());
                System.out.println("RESULT=" + ((Flux<String>) returned).collectList().block(java.time.Duration.ofSeconds(10)));
            }
            case "mono_subtype" -> System.out.println("RESULT=" + service.subtype().block(java.time.Duration.ofSeconds(10)));
            case "async_cancel" -> { var pending = service.neverCompletes("q"); pending.cancel(true); System.out.println("RESULT=cancelled=" + pending.isCancelled()); }
            case "nested_own_span" -> System.out.println("RESULT=" + service.outerWithOwnSpan(args[1]));
            case "own_span" -> System.out.println("RESULT=" + service.withOwnSpan(args[1]));
            case "bridge" -> System.out.println("RESULT=" + ((java.util.function.Function<String, String>) new Doubler()).apply("ab"));
            case "default_method" -> System.out.println("RESULT=" + new Greeter().greet("g"));
            case "chars" -> System.out.println("RESULT=" + service.login("user", System.getenv("FI_API_KEY").toCharArray(), System.getenv("FI_API_KEY").getBytes()));
            case "single_use" -> {
                java.util.concurrent.atomic.AtomicInteger iterators = new java.util.concurrent.atomic.AtomicInteger();
                Iterable<String> once = () -> { iterators.incrementAndGet(); return List.of("a", "b", "c").iterator(); };
                System.out.println("RESULT=" + service.consume(once) + ";iterators=" + iterators.get());
            }
            case "cyclic" -> System.out.println("RESULT=" + service.walk(Service.cyclic(), Service.huge()));
            case "flux_take" -> System.out.println("RESULT=" + service.flux(10).take(2).collectList().block(java.time.Duration.ofSeconds(10)));
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

        @Agent(name = "planner")
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

        /** An LLM-kind method that makes a real provider HTTP call: the agent's wire span must still appear, under this one. */
        @LLM(name = "ask_model")
        public String callModel(String endpoint) throws Exception {
            var client = java.net.http.HttpClient.newHttpClient();
            var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create(endpoint))
                .header("Content-Type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString("{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}")).build();
            return String.valueOf(client.send(request, java.net.http.HttpResponse.BodyHandlers.ofString()).statusCode());
        }

        /** An annotated method whose body starts the application's own OpenTelemetry span and makes a provider call inside it. */
        @Chain(name = "own_span_chain")
        public String withOwnSpan(String endpoint) throws Exception {
            io.opentelemetry.api.trace.Span own = io.opentelemetry.sdk.trace.SdkTracerProvider.builder().build().get("app").spanBuilder("app-inner").startSpan();
            System.out.println("APP_SPAN=" + own.getSpanContext().getSpanId());
            try (io.opentelemetry.context.Scope scope = own.makeCurrent()) {
                var client = java.net.http.HttpClient.newHttpClient();
                var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create(endpoint))
                    .header("Content-Type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString("{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}")).build();
                return String.valueOf(client.send(request, java.net.http.HttpResponse.BodyHandlers.ofString()).statusCode());
            } finally { own.end(); }
        }

        /** @Agent, then the application's own span Y, then a @Tool that makes the provider call: the wire span belongs under the tool. */
        @Agent(name = "outer_agent")
        public String outerWithOwnSpan(String endpoint) throws Exception {
            io.opentelemetry.api.trace.Span own = io.opentelemetry.sdk.trace.SdkTracerProvider.builder().build().get("app").spanBuilder("app-middle").startSpan();
            System.out.println("APP_SPAN=" + own.getSpanContext().getSpanId());
            try (io.opentelemetry.context.Scope scope = own.makeCurrent()) { return nestedTool(endpoint); } finally { own.end(); }
        }

        @Tool(name = "nested_tool")
        public String nestedTool(String endpoint) throws Exception { return callModelPlain(endpoint); }

        private String callModelPlain(String endpoint) throws Exception {
            var client = java.net.http.HttpClient.newHttpClient();
            var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create(endpoint))
                .header("Content-Type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString("{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}")).build();
            return String.valueOf(client.send(request, java.net.http.HttpResponse.BodyHandlers.ofString()).statusCode());
        }

        @Chain(name = "audit")
        public String audit(Object a, Object b, Object c, Object d, Object e) { return "ok"; }

        @Chain(name = "sink")
        public String sink(String text) { return "ok"; }

        /** Returns what it is given: the text is exported as an input and as an output. */
        @Chain(name = "echo")
        public String echo(String text) { return text; }

        public record Login(String user, String password, String pwd, String pass, String passphrase, String apiToken, String note) { }
        @Chain(name = "sign_in")
        public String signIn(Login login) { return "ok"; }

        /** A Flux the application defines, returned through a declared type that is not exactly Flux. */
        public static final class AppFlux extends Flux<String> {
            @Override public void subscribe(reactor.core.CoreSubscriber<? super String> actual) { Flux.just("a", "b").subscribe(actual); }
        }
        static final AppFlux PUBLISHED = new AppFlux();
        @Chain(name = "as_object")
        public Object asObject() { return PUBLISHED; }
        @Chain(name = "as_publisher")
        public org.reactivestreams.Publisher<String> asPublisher() { return PUBLISHED; }

        public static final class GreetingMono extends Mono<String> {
            @Override public void subscribe(reactor.core.CoreSubscriber<? super String> actual) { Mono.just("g").subscribe(actual); }
        }
        @Chain(name = "subtype_mono")
        public GreetingMono subtype() { return new GreetingMono(); }

        @Chain(name = "never_completes")
        public java.util.concurrent.CompletableFuture<String> neverCompletes(String q) { return new java.util.concurrent.CompletableFuture<>(); }

        @Span(kind = " ")
        public String blank() { return "b"; }

        @Span(kind = "guardrail")
        @Tool
        public String both() { return "t"; }

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

        @Tool(name = "login")
        public String login(String user, char[] chars, byte[] bytes) { return user + chars.length + bytes.length; }

        /** Reads the iterable itself: it is single use, so the span must not have consumed it. */
        @Chain
        public int consume(Iterable<String> items) { int n = 0; for (String ignored : items) n++; return n; }

        public record Cell(String label, List<Object> peers) { }
        private static final class Hidden { private final String apiToken = "private-field-token"; }
        static Cell cyclic() { List<Object> peers = new java.util.ArrayList<>(); Cell cell = new Cell("c", peers); peers.add(cell); peers.add(new Hidden()); return cell; }
        static List<Object> huge() {
            List<Object> rows = new java.util.ArrayList<>(); String big = "lorem ipsum dolor sit amet ".repeat(40_000);
            for (int i = 0; i < 10; i++) rows.add(big);
            return rows;
        }
        @Chain
        public int walk(Cell cell, List<Object> rows) { return cell.peers().size() + rows.size(); }
    }

    /** A generic override: javac adds a bridge apply(Object) that carries the same annotation. */
    public static final class Doubler implements java.util.function.Function<String, String> {
        @Tool(name = "doubler")
        @Override public String apply(String value) { return value + value; }
    }

    public interface Hello {
        @Tool(name = "greeter")
        default String greet(String who) { return "hello " + who; }
    }
    public static final class Greeter implements Hello { }
    private AnnotatedApp() { }

    /** Free-form text with a credential in every shape that has leaked through a reader that parsed it. */
    static List<String> credentialStrings() {
        String basic = java.util.Base64.getEncoder().encodeToString("user:SYNTHB64PASS".getBytes());
        return List.of(
            "authorization=Basic " + basic + " trailing words",
            "Authorization: Basic " + basic,
            "password=SYNTHPAREN)TAILPAREN, id=7",
            "password=[SYNTHBRACK, ], TAILBRACK] id=8",
            "secret=\"SYNTHQUOTE\"TAILQUOTE\" id=9",
            "run --password=SYNTHDASH now",
            "java -Ddb.password=SYNTHDOTD -jar app.jar",
            "pwd=SYNTHPWD id=10",
            "passphrase=SYNTHPHRASE id=11",
            "pass=SYNTHPASS id=12");
    }

    /** Application objects whose declared toString() carries a credential in a shape a naive reader would cut short. */
    public static final class Account { @Override public String toString() { return "Account(user=bob, password=[S, Y, N, T, H, 7], id=7)"; } }
    public static final class KeyHolder { @Override public String toString() { return "KeyHolder(privateKey=-----BEGIN PRIVATE KEY-----\nMIISYNTHPEMBODY0123\nSYNTHSECONDLINE\n-----END PRIVATE KEY-----, id=1)"; } }
    public static final class Csv { @Override public String toString() { return "Csv(secret=SYNTHA,SYNTHB,SYNTHC, id=2)"; } }
    public static final class Wrapped { @Override public String toString() { return "Wrapped(apiKey=Inner(value=SYNTHX, other=SYNTHY), id=3)"; } }
    public static final class BasicHolder { @Override public String toString() { return "BasicHolder(authorization=Basic " + java.util.Base64.getEncoder().encodeToString("user:SYNTHB64PASS".getBytes()) + ", id=5)"; } }
    public static final class CharsHolder { @Override public String toString() { return "CharsHolder(password=[SYNTHBRACK, ], TAILBRACK], id=6)"; } }
    public static final class ParenHolder { @Override public String toString() { return "ParenHolder(password=SYNTHPAREN)TAILPAREN, id=7)"; } }
    public static final class QuoteHolder { @Override public String toString() { return "QuoteHolder(secret='SYNTHQUOTE'TAILQUOTE', id=8)"; } }
    public static final class FlagHolder { @Override public String toString() { return "java --password=SYNTHDASH -Ddb.password=SYNTHDOTD pwd=SYNTHPWD"; } }
    public static final class Quoted { @Override public String toString() { return "Quoted(token=\"SYNTHA,SYNTHB\", id=4)"; } }
}
