package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;
import static org.junit.jupiter.api.Assertions.*;

/** Annotated application methods become typed spans, with the agent attached and with the one-line initialization. */
class AnnotationsE2ETest {
    static final String APP = "ai.traceai.apptest.AnnotatedApp";
    HttpServer server;
    ExecutorService executor;
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();

    @BeforeEach void startCollector() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/chat/completions", exchange -> {
            exchange.getRequestBody().readAllBytes(); byte[] body = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body); exchange.close();
        });
        server.createContext("/tracer/v1/traces", exchange -> {
            Object root = WireCore.parse(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            exchange.getResponseBody().write("{}".getBytes(StandardCharsets.UTF_8)); exchange.close();
        });
        executor = Executors.newFixedThreadPool(2); server.setExecutor(executor); server.start();
    }
    @AfterEach void stopCollector() { server.stop(0); executor.shutdownNow(); }

    /** mode: agent (-javaagent), init (agent jar on the class path, initialization first), none (no agent at all). */
    String run(String mode, String scenario, Map<String, String> env) throws Exception {
        return run(mode, scenario, env, List.of());
    }
    String run(String mode, String scenario, Map<String, String> env, List<String> extra) throws Exception {
        Path agent = AgentE2ETest.agentJar();
        String cp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        String classes = Path.of("target", "classes").toAbsolutePath().normalize().toString();
        List<String> app = new ArrayList<>(Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
            .filter(p -> !Path.of(p).toAbsolutePath().normalize().toString().equals(classes)).toList());
        List<String> command = new ArrayList<>();
        command.add(Path.of(System.getProperty("traceai.test.java.home", System.getProperty("java.home")), "bin", "java").toString());
        if (mode.equals("agent") || mode.equals("agent_isolated")) command.add("-javaagent:" + agent);
        if (mode.equals("init")) {
            // Only what an application using the agent jar as a library has: the jar, its own classes, the annotations, Reactor.
            app = new ArrayList<>(app.stream().filter(p -> {
                String name = Path.of(p).getFileName().toString();
                return name.equals("test-classes") || name.startsWith("traceai-java-annotations") || name.startsWith("reactor-core") || name.startsWith("reactive-streams");
            }).toList());
            app.add(0, agent.toString());
        }
        if (mode.equals("isolated") || mode.equals("agent_isolated")) {
            // The system class path holds only the launcher; the agent jar is in the loader the application runs in.
            List<String> inner = new ArrayList<>(app.stream().filter(p -> {
                String name = Path.of(p).getFileName().toString();
                return name.equals("test-classes") || name.startsWith("traceai-java-annotations") || name.startsWith("reactor-core") || name.startsWith("reactive-streams");
            }).toList());
            inner.add(0, agent.toString());
            command.addAll(List.of("-XX:+EnableDynamicAgentLoading", "-cp", Path.of("target", "test-classes").toAbsolutePath().toString(), "ai.traceai.apptest.IsolatedLauncher", scenario, String.join(File.pathSeparator, inner)));
        } else
        command.addAll(List.of("-XX:+EnableDynamicAgentLoading", "-cp", String.join(File.pathSeparator, app), APP, scenario));
        command.addAll(extra);
        Path output = Files.createTempFile(Path.of("target"), "annotations-e2e-", ".log");
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> environment = pb.environment(); environment.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        environment.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); environment.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        environment.put("FI_API_KEY", "test-api-key"); environment.put("FI_SECRET_KEY", "test-secret-key");
        environment.putAll(env);
        Process process = pb.start();
        if (!process.waitFor(40, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5); // the final export can trail the child's exit by a moment
        while (spans.isEmpty() && !mode.equals("none") && System.nanoTime() < deadline) Thread.sleep(50);
        Thread.sleep(300);
        return log;
    }

    static Map<String, Object> attrs(Map<String, Object> span) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (Object a : WireCore.list(span.get("attributes"))) {
            Map<String, Object> value = WireCore.map(WireCore.get(a, "value"));
            out.put(String.valueOf(WireCore.get(a, "key")), value.values().iterator().next());
        }
        return out;
    }
    Map<String, Object> span(String name) {
        List<Map<String, Object>> found = spans.stream().filter(s -> name.equals(s.get("name"))).toList();
        assertEquals(1, found.size(), "exactly one span named " + name + " in " + spans.stream().map(s -> s.get("name")).toList());
        return found.get(0);
    }
    Map<String, Object> parentOf(Map<String, Object> span) {
        Object parent = span.get("parentSpanId");
        return spans.stream().filter(s -> s.get("spanId").equals(parent)).findFirst().orElse(null);
    }
    /** The parsed JSON with integral numbers as Long and containers as plain collections, so it compares with Map.of / List.of. */
    static Object json(Object text) { return normal(WireCore.parse(String.valueOf(text))); }
    static Object normal(Object value) {
        if (value instanceof Map<?, ?> map) { Map<String, Object> out = new LinkedHashMap<>(); map.forEach((k, v) -> out.put(String.valueOf(k), normal(v))); return out; }
        if (value instanceof List<?> list) return list.stream().map(AnnotationsE2ETest::normal).toList();
        if (value instanceof Number number && number.doubleValue() == Math.rint(number.doubleValue())) return number.longValue();
        return value;
    }
    static List<String> names(List<Map<String, Object>> spans) { return spans.stream().map(s -> String.valueOf(s.get("name"))).sorted().toList(); }

    void assertSyncSpans(String log) {
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans));
        Map<String, Object> planner = span("planner"), tool = span("calculator"), llm = span("generate"), fetch = span("fetch_docs");
        assertEquals("AGENT", attrs(planner).get("fi.span.kind")); assertEquals("agent", attrs(planner).get("gen_ai.span.kind"));
        assertEquals("TOOL", attrs(tool).get("fi.span.kind")); assertEquals("LLM", attrs(llm).get("fi.span.kind"));
        assertEquals("RETRIEVER", attrs(fetch).get("fi.span.kind"));
        assertEquals("CHAIN", attrs(span("count")).get("fi.span.kind"));
        assertNull(planner.get("parentSpanId"), "the outermost annotated call is a root");
        for (Map<String, Object> child : List.of(tool, llm, fetch)) assertSame(planner, parentOf(child), "nested annotated calls parent under the current span");
        assertEquals("calculator", attrs(tool).get("gen_ai.tool.name")); assertEquals("calculator", attrs(tool).get("tool.name"));
        assertEquals("adds numbers", attrs(tool).get("gen_ai.tool.description"));
        // input.value: JSON of the non-excluded arguments, keyed by parameter name; the excluded one is absent everywhere.
        assertEquals(Map.of("question", "what is 2+2?"), json(attrs(planner).get("input.value")));
        assertEquals("application/json", attrs(planner).get("input.mime_type"));
        assertEquals("docs1|The answer is 4|" + AnnotatedAppSecret.LENGTH, json(attrs(planner).get("output.value")));
        assertEquals(Map.of("expression", "what is 2+2?"), json(attrs(tool).get("input.value")));
        assertEquals(Map.of("terms", List.of("what is 2+2?")), json(attrs(fetch).get("input.value")));
        assertEquals(Map.of("n", 41L), json(attrs(span("count")).get("input.value")));
        assertEquals(42, ((Number)json(attrs(span("count")).get("output.value"))).intValue());
        assertNull(attrs(span("noArguments")).get("input.value")); assertNull(attrs(span("noArguments")).get("output.value"));
        for (Map<String, Object> s : spans) {
            assertFalse(String.valueOf(s.get("attributes")).contains(AnnotatedAppSecret.VALUE), "excluded argument must not be exported");
            assertNotEquals(2, ((Number)WireCore.map(s.get("status")).get("code")).intValue(), s.get("name") + " must not be an error span");
        }
    }

    @Test void agentAttachedAnnotatedMethodsAreTraced() throws Exception { assertSyncSpans(run("agent", "sync", Map.of())); }
    @Test void oneLineInitializationTracesWithoutTheAgentFlag() throws Exception { assertSyncSpans(run("init", "init_first", Map.of())); }
    @Test void aClassLoadedBeforeInitializationIsStillTraced() throws Exception {
        run("init", "late_class_load", Map.of());
        assertEquals(List.of("calculator", "fetch_docs", "generate", "planner"), names(spans));
    }
    @Test void callingTheInstallerWithTheAgentAttachedDoesNotDoubleSpans() throws Exception {
        run("agent", "init_twice", Map.of());
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans));
    }
    @Test void exceptionsAreRecordedAndRethrownUnchanged() throws Exception {
        String log = run("agent", "error", Map.of());
        assertTrue(log.contains("CAUGHT=bad x " + AnnotatedAppSecret.VALUE + ";same=true"), log);
        Map<String, Object> boom = span("boom");
        assertEquals(2, ((Number)WireCore.map(boom.get("status")).get("code")).intValue(), "ERROR status");
        assertEquals("java.lang.IllegalStateException", attrs(boom).get("error.type"));
        assertFalse(WireCore.list(boom.get("events")).isEmpty(), "the exception is recorded as an event");
    }
    @Test void aThrowingMethodWithErrorDetailsHiddenRecordsOnlyTheType() throws Exception {
        run("agent", "error", Map.of("FI_HIDE_ERROR_DETAILS", "true"));
        Map<String, Object> boom = span("boom");
        assertEquals("", WireCore.map(boom.get("status")).get("message"));
        assertFalse(String.valueOf(boom).contains("bad x"), "no exception message when error details are hidden");
    }
    @Test void completableFutureSpansEndOnCompletion() throws Exception {
        run("agent", "async", Map.of());
        Map<String, Object> later = span("later");
        long start = Long.parseLong((String)later.get("startTimeUnixNano")), end = Long.parseLong((String)later.get("endTimeUnixNano"));
        assertTrue(end - start >= 250_000_000L, "the span covers the asynchronous work, not just the method call: " + (end - start));
        assertEquals("later:q", json(attrs(later).get("output.value")));
    }
    @Test void aFailedCompletableFutureIsAnErrorSpan() throws Exception {
        run("agent", "async_error", Map.of());
        Map<String, Object> failed = span("laterFails");
        assertEquals(2, ((Number)WireCore.map(failed.get("status")).get("code")).intValue());
    }
    @Test void reactorMonoAndFluxSpansEndOnTermination() throws Exception {
        run("agent", "mono", Map.of());
        Map<String, Object> mono = span("mono");
        assertTrue(Long.parseLong((String)mono.get("endTimeUnixNano")) - Long.parseLong((String)mono.get("startTimeUnixNano")) >= 150_000_000L);
        assertEquals("mono:m", json(attrs(mono).get("output.value")));
        spans.clear();
        run("agent", "flux", Map.of());
        assertEquals(List.of(1L, 2L, 3L), json(attrs(span("flux")).get("output.value")));
    }
    @Test void hideInputsDropsInputValueButKeepsTheToolName() throws Exception {
        run("agent", "sync", Map.of("FI_HIDE_INPUTS", "true"));
        for (Map<String, Object> s : spans) { assertNull(attrs(s).get("input.value"), s.get("name") + " input"); assertNotNull(s.get("name")); }
        assertNotNull(attrs(span("planner")).get("output.value"), "outputs are still recorded");
        assertEquals("calculator", attrs(span("calculator")).get("gen_ai.tool.name"), "the tool name is not withheld");
    }
    @Test void hideOutputsDropsOutputValue() throws Exception {
        run("agent", "sync", Map.of("FI_HIDE_OUTPUTS", "true"));
        for (Map<String, Object> s : spans) assertNull(attrs(s).get("output.value"), s.get("name") + " output");
        assertNotNull(attrs(span("planner")).get("input.value"));
    }
    @Test void aKnownSecretInAnArgumentIsReplacedOnExport() throws Exception {
        run("agent", "secret", Map.of("FI_API_KEY", AnnotatedAppSecret.KEY));
        // The application passes its own registered key as the question; the export never carries it.
        assertFalse(spans.isEmpty());
        assertFalse(spans.toString().contains(AnnotatedAppSecret.KEY), "the registered key is replaced in every exported span");
        assertNotNull(attrs(span("planner")).get("input.value"), "the span is still exported");
    }
    @Test void withoutTheAgentTheApplicationRunsAndNothingIsExported() throws Exception {
        String log = run("none", "sync", Map.of());
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertTrue(spans.isEmpty());
    }
    @Test void aProviderCallInsideAnLlmMethodKeepsItsWireSpanUnderTheAnnotatedSpan() throws Exception {
        run("agent", "wire", Map.of(), List.of("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions"));
        Map<String, Object> annotated = span("ask_model");
        List<Map<String, Object>> wire = spans.stream().filter(s -> !"ask_model".equals(s.get("name"))).toList();
        assertEquals(1, wire.size(), "the provider call is still traced: " + names(spans));
        assertSame(annotated, parentOf(wire.get(0)), "the wire span nests under the annotated span");
        assertEquals("LLM", attrs(annotated).get("fi.span.kind"));
    }
    @Test void aBlankSpanKindIsChainAndTheFirstAnnotationInPriorityOrderWins() throws Exception {
        run("agent", "blank_kind", Map.of());
        assertEquals("CHAIN", attrs(span("blank")).get("fi.span.kind"));
        assertEquals("TOOL", attrs(span("both")).get("fi.span.kind"), "@Tool outranks @Span on one method; one span only");
        assertEquals("both", attrs(span("both")).get("tool.name"));
    }
    @Test void disablingAnnotationsTurnsTracingOffWithoutChangingTheApplication() throws Exception {
        String log = run("agent", "sync", Map.of("FI_DISABLED_INSTRUMENTATIONS", "annotations"));
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertTrue(spans.isEmpty(), "no span: " + names(spans));
    }
    @Test void aGenericOverrideWithAToolAnnotationIsOneSpan() throws Exception {
        String log = run("agent", "bridge", Map.of());
        assertTrue(log.contains("RESULT=abab"), log);
        assertEquals(List.of("doubler"), names(spans), "the compiler's bridge method must not add a second, nested span");
    }
    @Test void aDefaultInterfaceMethodIsTraced() throws Exception {
        String log = run("agent", "default_method", Map.of());
        assertTrue(log.contains("RESULT=hello g"), log);
        assertEquals(List.of("greeter"), names(spans));
        assertEquals("TOOL", attrs(span("greeter")).get("fi.span.kind"));
    }
    @Test void charAndByteArrayArgumentsAreAPlaceholderAndNeverTheirContent() throws Exception {
        run("agent", "chars", Map.of("FI_API_KEY", AnnotatedAppSecret.KEY));
        assertEquals(Map.of("user", "user", "chars", "<char[" + AnnotatedAppSecret.KEY.length() + "]>", "bytes", "<byte[" + AnnotatedAppSecret.KEY.length() + "]>"), json(attrs(span("login")).get("input.value")));
        assertFalse(spans.toString().contains(AnnotatedAppSecret.KEY));
    }
    @Test void aSingleUseIterableArgumentIsStillFullyReadableByTheMethod() throws Exception {
        String log = run("agent", "single_use", Map.of());
        assertTrue(log.contains("RESULT=3;iterators=1"), "the method read all three items, the span did not iterate: " + log);
        assertTrue(String.valueOf(((Map<?, ?>)json(attrs(span("consume")).get("input.value"))).get("items")).startsWith("<"), "a type placeholder");
        assertFalse(String.valueOf(attrs(span("consume")).get("input.value")).contains("\"a\""));
    }
    @Test void aCyclicOrHugeArgumentIsBoundedAndPrivateFieldsAreNotExported() throws Exception {
        String log = run("agent", "cyclic", Map.of());
        assertTrue(log.contains("RESULT=12"), log);
        String input = String.valueOf(attrs(span("walk")).get("input.value"));
        assertTrue(input.contains("<cycle>"), input);
        assertFalse(input.contains("private-field-token"), "private fields are not reflected: " + input);
        assertTrue(input.length() <= 65_536 + 32, "input length " + input.length());
    }
    @Test void aCancelledFluxEndsItsSpanWithWhatWasEmittedAndACancelledMarker() throws Exception {
        String log = run("agent", "flux_take", Map.of());
        assertTrue(log.contains("RESULT=[1, 2]"), log);
        Map<String, Object> flux = span("flux");
        assertEquals(List.of(1L, 2L), json(attrs(flux).get("output.value")));
        assertEquals("true", String.valueOf(attrs(flux).get("traceai.cancelled")));
    }
    @Test void hideInputsAlsoWithholdsTheToolDescription() throws Exception {
        run("agent", "sync", Map.of("FI_HIDE_INPUTS", "true"));
        assertNull(attrs(span("calculator")).get("gen_ai.tool.description"));
    }
    @Test void anAgentJarOnlyInTheApplicationsOwnClassLoaderStillTracesAnnotatedMethods() throws Exception {
        String log = run("isolated", "init_first", Map.of());
        assertFalse(log.contains("AGENT_ON_SYSTEM_CLASS_PATH"), "the premise: the jar is not on the system class path: " + log);
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans));
    }
    @Test void anApplicationSpanStartedInsideAnAnnotatedMethodIsTheParentOfTheWireCall() throws Exception {
        String log = run("agent", "own_span", Map.of(), List.of("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions"));
        String own = java.util.Arrays.stream(log.split("\\R")).filter(l -> l.startsWith("APP_SPAN=")).findFirst().orElseThrow().substring("APP_SPAN=".length());
        Map<String, Object> chain = span("own_span_chain");
        List<Map<String, Object>> wire = spans.stream().filter(s -> !"own_span_chain".equals(s.get("name"))).toList();
        assertEquals(1, wire.size(), names(spans).toString());
        assertEquals(own, wire.get(0).get("parentSpanId"), "the application's current span wins over the enclosing annotated span");
        assertNull(chain.get("parentSpanId"));
    }

    /** -javaagent plus a separate-loader copy of the engine calling init(): one transformer per JVM, so one span per call. */
    @Test void anAgentFlagPlusAnIsolatedCopyThatInitializesStillGivesOneSpanPerCall() throws Exception {
        String log = run("agent_isolated", "init_first", Map.of());
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans), "no method is traced twice");
    }
    @Test void aNestedAnnotatedCallRecordsTheApplicationSpanCurrentAtItsOwnEntry() throws Exception {
        String log = run("agent", "nested_own_span", Map.of(), List.of("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions"));
        Map<String, Object> tool = span("nested_tool");
        List<Map<String, Object>> wire = spans.stream().filter(s -> !List.of("outer_agent", "nested_tool").contains(s.get("name"))).toList();
        assertEquals(1, wire.size(), names(spans).toString() + log);
        assertSame(tool, parentOf(wire.get(0)), "the provider call nests under the @Tool span, not under the application span around it");
    }
    @Test void theFiInstrumentationsAllowlistAppliesToAnnotations() throws Exception {
        run("agent", "sync", Map.of("FI_INSTRUMENTATIONS", "okhttp"));
        assertTrue(spans.isEmpty(), "annotations are not on the allowlist: " + names(spans));
    }
    @Test void aCancelledCompletableFutureEndsItsSpanWithACancelledMarkerNotAnError() throws Exception {
        String log = run("agent", "async_cancel", Map.of());
        assertTrue(log.contains("RESULT=cancelled=true"), log);
        Map<String, Object> pending = span("never_completes");
        assertEquals("true", String.valueOf(attrs(pending).get("traceai.cancelled")));
        assertNotEquals(2, ((Number)WireCore.map(pending.get("status")).get("code")).intValue(), "a cancel is not an error");
    }

    /** Every assertion below is on the span bytes the collector received: no byte of a synthetic credential reaches the exporter. */
    String exported() { return spans.toString(); }

    @Test void aCredentialInADeclaredToStringNeverReachesTheExporterWhateverItsShape() throws Exception {
        String log = run("agent", "credential_text", Map.of());
        assertTrue(log.contains("RESULT=ok"), log);
        String bytes = exported();
        assertFalse(spans.isEmpty());
        for (String marker : List.of("S, Y, N, T, H", "Y, N", "SYNTH", "MIISYNTHPEMBODY", "BEGIN PRIVATE KEY", "END PRIVATE KEY", ",SYNTHB", "SYNTHX", "SYNTHY"))
            assertFalse(bytes.contains(marker), marker + " reached the exporter: " + bytes);
        assertTrue(bytes.contains("id=7") && bytes.contains("id=4"), "the ordinary pairs stay visible: " + bytes);
    }

    /** A known secret twice, a JWT, an sk- key and a Bearer token, with the cut sweeping across all of them one character at a time. */
    @Test void secretsAndShapedCredentialsStraddlingTheCutNeverReachTheExporter() throws Exception {
        String secret = "SYNTH-TWICE-SECRET-0042-XYZ";
        String log = run("agent", "straddle", Map.of("FI_API_KEY", secret, "FI_WIRE_MAX_BODY_BYTES", "400"));
        assertTrue(log.contains("RESULT="), log);
        assertTrue(spans.size() > 100, "one span per cut position: " + spans.size());
        long cut = spans.stream().filter(s -> String.valueOf(s).contains("truncated")).count();
        assertTrue(cut > 100, "the cut really moved across the credentials: " + cut);
        for (Map<String, Object> span : spans) {
            String bytes = String.valueOf(span);
            for (String marker : List.of("SYNTH", "eyJ", "sk-S", "Bearer SY"))
                assertFalse(bytes.contains(marker), marker + " reached the exporter in " + attrs(span).get("input.value"));
        }
    }

    @Test void aCopyWithAnnotationsDisabledDoesNotBlockAnotherCopyThatHasThemEnabled() throws Exception {
        String log = run("agent_isolated", "init_enabled", Map.of("FI_DISABLED_INSTRUMENTATIONS", "annotations"));
        assertTrue(log.contains("RESULT=docs1|The answer is 4|"), log);
        assertEquals(List.of("calculator", "count", "fetch_docs", "generate", "noArguments", "planner"), names(spans), "the second copy traces");
    }

    @Test void aMethodDeclaredToReturnAMonoSubtypeStillEndsItsSpan() throws Exception {
        String log = run("agent", "mono_subtype", Map.of());
        assertTrue(log.contains("RESULT=g"), log);
        assertEquals("subtype_mono", String.valueOf(span("subtype_mono").get("name")), "the span ended and was exported");
    }

    /** Observed, not changed: with an agent flag plus a separate-loader copy, a provider call is exactly one wire span. */
    @Test void aProviderCallWithTwoEngineCopiesIsOneWireSpan() throws Exception {
        run("agent_isolated", "wire", Map.of(), List.of("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/chat/completions"));
        List<Map<String, Object>> wire = spans.stream().filter(s -> !"ask_model".equals(s.get("name"))).toList();
        assertEquals(1, wire.size(), "double wire span? " + names(spans));
    }
}
