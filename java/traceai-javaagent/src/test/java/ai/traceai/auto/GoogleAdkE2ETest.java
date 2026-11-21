package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.*;

import java.io.File;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.*;

/** The packaged agent, in a child JVM, traces a real Google ADK for Java agent with no application code (AdkApp). */
class GoogleAdkE2ETest {
    static final String PROMPT = "SENTINEL-PROMPT-weather-in-Kyoto";
    static final String ANSWER = "SENTINEL-ANSWER-sunny";
    static final String TOOL_RESULT = "SENTINEL-TOOL-RESULT";

    /** ADK 1.0.0 ends the invocation span before the agent span it contains, so that span cannot carry what the agent span reports. */
    boolean adkVersion100;
    /** The child's main class: AdkApp (a stub model), or AdkGeminiApp (ADK's own Gemini client). */
    String appClass = "ai.traceai.googleadk.AdkApp";
    final java.util.concurrent.atomic.AtomicInteger geminiCalls = new java.util.concurrent.atomic.AtomicInteger();
    final List<String> geminiBodies = new CopyOnWriteArrayList<>();
    final List<String> geminiRequests = new CopyOnWriteArrayList<>();
    /** A directory added to the child's class path, or null. */
    Path extraClasspath;
    HttpServer server;
    ExecutorService executor;
    final java.util.concurrent.atomic.AtomicInteger modelCalls = new java.util.concurrent.atomic.AtomicInteger();
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();
    /** Every export request body as the collector received it. */
    final List<String> exported = new CopyOnWriteArrayList<>();

    @BeforeEach void startServer() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/chat/completions", exchange -> {
            exchange.getRequestBody().readAllBytes(); modelCalls.incrementAndGet();
            byte[] body = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, body.length);
            try (var out = exchange.getResponseBody()) { out.write(body); }
        });
        // The Gemini generateContent REST shape that google-genai sends: POST {baseUrl}/v1beta/models/{model}:generateContent.
        server.createContext("/v1beta/models/", exchange -> {
            String request = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            geminiCalls.incrementAndGet(); geminiBodies.add(request);
            geminiRequests.add(exchange.getRequestMethod() + " " + exchange.getRequestURI() + " key=" + exchange.getRequestHeaders().getFirst("x-goog-api-key"));
            String part = request.contains("functionResponse")
                ? "{\"text\":\"" + ANSWER + "\"}"
                : "{\"functionCall\":{\"name\":\"weather\",\"args\":{\"city\":\"Kyoto\"},\"id\":\"call-1\"}}";
            byte[] body = ("{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[" + part + "]},\"finishReason\":\"STOP\"}],"
                + "\"usageMetadata\":{\"promptTokenCount\":11,\"candidatesTokenCount\":7,\"totalTokenCount\":18}}").getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, body.length);
            try (var out = exchange.getResponseBody()) { out.write(body); }
        });
        server.createContext("/tracer/v1/traces", exchange -> {
            String payload = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8); exported.add(payload);
            Object root = WireCore.parse(payload);
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans")))
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans")))
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2);
            try (var out = exchange.getResponseBody()) { out.write("{}".getBytes(StandardCharsets.UTF_8)); }
        });
        executor = Executors.newFixedThreadPool(3); server.setExecutor(executor); server.start();
    }
    @AfterEach void stopServer() { server.stop(0); executor.shutdownNow(); }

    String run(String mode, String prompt, Map<String, String> options) throws Exception { return run(mode, prompt, options, "subscribe"); }

    String run(String mode, String prompt, Map<String, String> options, String send) throws Exception { return run(mode, prompt, options, send, "single"); }

    String run(String mode, String prompt, Map<String, String> options, String send, String scenario) throws Exception {
        Path agent = AgentE2ETest.agentJar(); assertTrue(Files.isRegularFile(agent));
        // The application is the google-adk module's test application (AdkApp) with ADK and its dependencies, which the
        // module's build lists; the agent's classes come only from the packaged jar.
        Path module = Path.of("..", "traceai-java-google-adk", "target").toAbsolutePath().normalize();
        Path classpath = module.resolve("adk-app.classpath");
        assertTrue(Files.isRegularFile(classpath), "build the traceai-java-google-adk module first (mvn -pl traceai-javaagent -am verify): " + classpath);
        // The module's own classes (TracedLlm, TracedTool) come before its dependencies.
        String cp = module.resolve("test-classes") + File.pathSeparator + module.resolve("classes") + File.pathSeparator + Files.readString(classpath).trim();
        if (extraClasspath != null) cp = extraClasspath + File.pathSeparator + cp;
        adkVersion100 = cp.contains("google-adk-1.0.0.jar");
        Path output = Files.createTempFile(Path.of("target"), "adk-e2e-", ".log");
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-javaagent:" + agent,
            "-cp", cp, appClass, mode, "http://127.0.0.1:" + server.getAddress().getPort(), prompt, send, scenario));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_") || k.startsWith("ADK_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key");
        env.putAll(options);
        Process process = pb.start();
        if (!process.waitFor(60, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        assertTrue(log.contains("DONE"), log);
        return log;
    }

    static Map<String, Object> attributes(Map<String, Object> span) { return IncomingRequestsE2ETest.attributes(span); }
    Map<String, Object> only(String name) {
        List<Map<String, Object>> found = spans.stream().filter(s -> name.equals(s.get("name"))).toList();
        assertEquals(1, found.size(), name + " in " + spans);
        return found.get(0);
    }
    /** The tool span: ADK 1.0.0 names it "execute_tool [weather]", later versions "execute_tool weather". */
    Map<String, Object> tool() {
        List<Map<String, Object>> found = named("execute_tool");
        assertEquals(1, found.size(), spans.toString());
        assertTrue(String.valueOf(found.get(0).get("name")).matches("execute_tool \\[?weather]?"), String.valueOf(found.get(0).get("name")));
        return found.get(0);
    }
    List<Map<String, Object>> named(String prefix) { return spans.stream().filter(s -> String.valueOf(s.get("name")).startsWith(prefix)).toList(); }
    List<Map<String, Object>> byLayer(String layer) { return spans.stream().filter(s -> layer.equals(attributes(s).get("traceai.capture.layer"))).toList(); }

    @Test void invocationAgentModelAndToolCallsAreSpansWithTheirAttributes() throws Exception {
        run("plain", PROMPT, Map.of());
        Map<String, Object> invocation = only("invocation"), agent = only("invoke_agent weather_agent"), tool = tool();
        List<Map<String, Object>> llm = named("call_llm");
        assertEquals(2, llm.size(), spans.toString());
        assertEquals(5, spans.size(), "no other span, the wire spans of the model's HTTP calls included: " + spans);
        // One trace, the parent chain ADK made.
        assertNull(invocation.get("parentSpanId"));
        assertEquals(invocation.get("spanId"), agent.get("parentSpanId"));
        for (Map<String, Object> call : llm) assertEquals(agent.get("spanId"), call.get("parentSpanId"));
        assertEquals(llm.stream().map(s -> s.get("spanId")).collect(Collectors.toSet()).contains(tool.get("parentSpanId")), true, "the tool runs under the call that asked for it");
        for (Map<String, Object> span : spans) assertEquals(invocation.get("traceId"), span.get("traceId"));

        Map<String, Object> top = attributes(invocation);
        assertEquals("CHAIN", top.get("gen_ai.span.kind"));
        if (!adkVersion100) {
            assertEquals("user-1", top.get("user.id"));
            assertNotNull(top.get("session.id"));
            assertEquals(PROMPT, top.get("input.value"));
            assertEquals(ANSWER, top.get("output.value"));
        }
        Map<String, Object> a = attributes(agent);
        assertEquals("AGENT", a.get("gen_ai.span.kind"));
        assertEquals("weather_agent", a.get("gen_ai.agent.name"));
        assertEquals("user-1", a.get("user.id"));
        assertNotNull(a.get("session.id"));
        assertEquals(PROMPT, a.get("input.value"));
        assertEquals(ANSWER, a.get("output.value"));

        Map<String, Object> second = llm.stream().map(GoogleAdkE2ETest::attributes).filter(x -> x.containsKey("gen_ai.input.messages.3.message.role")).findFirst().orElseThrow();
        assertEquals("LLM", second.get("gen_ai.span.kind"));
        assertEquals("gemini-2.0-flash", second.get("gen_ai.request.model"));
        assertEquals("system", second.get("gen_ai.input.messages.0.message.role"));
        assertEquals(PROMPT, second.get("gen_ai.input.messages.1.message.content"));
        assertEquals("weather", second.get("gen_ai.input.messages.2.message.tool_calls.0.tool_call.function.name"));
        assertEquals("tool", second.get("gen_ai.input.messages.3.message.role"));
        assertEquals(ANSWER, second.get("gen_ai.output.messages.0.message.content"));
        assertEquals("11", String.valueOf(second.get("gen_ai.usage.input_tokens")));
        assertEquals("7", String.valueOf(second.get("gen_ai.usage.output_tokens")));
        assertTrue(String.valueOf(second.get("gen_ai.tool.definitions.0.tool.json_schema")).contains("weather"));
        Map<String, Object> first = llm.stream().map(GoogleAdkE2ETest::attributes).filter(x -> !x.containsKey("gen_ai.input.messages.3.message.role")).findFirst().orElseThrow();
        assertEquals("call-1", first.get("gen_ai.output.messages.0.message.tool_calls.0.tool_call.id"));

        Map<String, Object> t = attributes(tool);
        assertEquals("TOOL", t.get("gen_ai.span.kind"));
        assertEquals("weather", t.get("gen_ai.tool.name"));
        assertEquals("call-1", t.get("gen_ai.tool.call.id"));
        assertTrue(String.valueOf(t.get("gen_ai.tool.call.arguments")).contains("Kyoto"));
        assertTrue(String.valueOf(t.get("gen_ai.tool.call.result")).contains(TOOL_RESULT));
    }

    @Test void theModelsHttpCallIsNotTracedASecondTime() throws Exception {
        run("plain", PROMPT, Map.of());
        assertEquals(2, modelCalls.get(), "the model stub made its two HTTP calls");
        assertEquals(List.of(), byLayer("wire"), "the wire capture stays out of an ADK model call: " + spans);
        assertTrue(spans.stream().noneMatch(s -> String.valueOf(s.get("name")).startsWith("chat ")), spans.toString());
    }

    /** The model client may send the HTTP call while ADK assembles the request: still not traced a second time. */
    @Test void theModelsHttpCallIsNotTracedASecondTimeWhenSentWhileAdkAssemblesTheRequest() throws Exception {
        run("plain", PROMPT, Map.of(), "assembly");
        assertEquals(2, modelCalls.get(), "the model stub made its two HTTP calls");
        assertEquals(2, named("call_llm").size(), "exactly one LLM span per model call: " + spans);
        assertEquals(List.of(), byLayer("wire"), "no wire span for the model's HTTP call: " + spans);
        assertEquals(5, spans.size(), spans.toString());
    }

    /**
     * The documented limit: a client that hops the model's request to a thread of its own executor sends it from a thread with no span, which
     * is never dropped (the registry cannot tell whose request it is). Each such request is a wire span beside the LLM span, with its own context.
     */
    @Test void aModelRequestHoppedToAnExecutorThreadIsRecordedAsAWireSpanBesideTheLlmSpan() throws Exception {
        run("plain", PROMPT, Map.of(), "executor");
        assertEquals(2, modelCalls.get(), "the model stub made its two HTTP calls");
        assertEquals(2, named("call_llm").size(), "exactly one LLM span per model call: " + spans);
        assertEquals(2, byLayer("wire").size(), "the documented duplicate: " + spans);
        for (Map<String, Object> wire : byLayer("wire")) assertNull(wire.get("parentSpanId"), "its own context: " + wire);
    }

    /** A call_llm open in an unrelated session must never drop an LLM call made under another span. */
    @Test void aToolsOwnLlmCallIsOneWireSpanUnderTheToolSpanWhileAnotherSessionsModelCallIsOpen() throws Exception {
        for (String mode : List.of("plain", "sdk")) {
            spans.clear(); modelCalls.set(0);
            run(mode, PROMPT, Map.of(), "subscribe", "tool-call");
            List<Map<String, Object>> wire = byLayer("wire");
            assertEquals(1, wire.size(), mode + ": exactly the tool's own HTTP call is a wire span: " + spans);
            // Under the TOOL span, or under the model call that asked for it when this ADK version runs the tool inside that call's scope.
            Object parent = wire.get(0).get("parentSpanId");
            assertTrue(parent.equals(tool().get("spanId")) || parent.equals(tool().get("parentSpanId")), mode + ": under the tool's own span or its parent: " + spans);
            assertEquals(3, named("call_llm").size(), mode + ": the two model calls of one session and the slow session's: " + spans);
        }
    }

    @Test void anUnrelatedThreadWithItsOwnSpanIsNeverSuppressedByAnotherSessionsModelCall() throws Exception {
        String log = run("sdk", PROMPT, Map.of(), "subscribe", "own-span");
        List<Map<String, Object>> wire = byLayer("wire");
        // The slow session's own model call is not a wire span; the application's call under its own span is.
        assertEquals(1, wire.size(), spans.toString());
        String appSpan = log.lines().filter(l -> l.startsWith("APP-SPAN ")).findFirst().orElseThrow().substring("APP-SPAN ".length()).trim();
        assertEquals(appSpan, wire.get(0).get("parentSpanId"), "under the application's span");
    }

    @Test void aFailedModelCallWithErrorDetailsHiddenCarriesNeitherTheErrorTextNorTheOutput() throws Exception {
        run("plain", PROMPT, Map.of("FI_HIDE_ERROR_DETAILS", "true"), "subscribe", "error");
        String everything = spans.toString();
        assertFalse(everything.contains("SEED-MODEL-ERROR"), everything);
        assertFalse(everything.contains("test-secret-key"), everything);
        Map<String, Object> llm = named("call_llm").get(0);
        assertEquals("", String.valueOf(((Map<?, ?>) llm.get("status")).get("message")), String.valueOf(llm.get("status")));
        assertEquals(2, ((Number) ((Map<?, ?>) llm.get("status")).get("code")).intValue(), "still an error");
        assertFalse(attributes(llm).containsKey("output.value"), "output of a failed call is withheld: " + llm);
        assertFalse(attributes(llm).containsKey("output.mime_type"), llm.toString());
        // The seeded output is not an FI key: it must be absent everywhere, in the messages and in the copies on the AGENT and CHAIN spans.
        assertFalse(everything.contains("SEED-FAILED-OUTPUT"), everything);
    }

    @Test void theResultOfAFailedToolCallIsRecordedOnlyWhileErrorDetailsAreShown() throws Exception {
        run("plain", PROMPT, Map.of(), "subscribe", "tool-error");
        assertTrue(String.valueOf(attributes(tool()).get("gen_ai.tool.call.result")).contains("SEED-TOOL-ERROR"), "control: " + spans);
        spans.clear();
        run("plain", PROMPT, Map.of("FI_HIDE_ERROR_DETAILS", "true"), "subscribe", "tool-error");
        Map<String, Object> tool = tool();
        assertFalse(tool.toString().contains("SEED-TOOL-ERROR"), tool.toString());
        for (String key : List.of("gen_ai.tool.call.result", "output.value", "output.mime_type")) assertFalse(attributes(tool).containsKey(key), key + " of a failed tool call is withheld: " + tool);
        assertEquals("weather", attributes(tool).get("gen_ai.tool.name"));
    }

    /** A span-less request (a thread of the application with no span) is never dropped because another context's model call is open. */
    @Test void aSpanLessRequestIsNeverDroppedByAnotherSessionsOpenModelCall() throws Exception {
        for (String mode : List.of("plain", "sdk")) {
            spans.clear(); modelCalls.set(0);
            run(mode, PROMPT, Map.of(), "subscribe", "span-less");
            List<Map<String, Object>> wire = byLayer("wire");
            assertEquals(1, wire.size(), mode + ": the span-less request is a wire span: " + spans);
            Map<String, Object> b = wire.get(0);
            // B's own context: no parent, and a trace of its own.
            assertNull(b.get("parentSpanId"), mode + ": " + b);
            List<Map<String, Object>> adk = byLayer("google-adk");
            assertFalse(adk.isEmpty());
            for (Map<String, Object> span : adk) assertNotEquals(b.get("traceId"), span.get("traceId"), mode + ": B shares no trace with the ADK spans");
            // The same redaction as any capture, and nothing of B in A's model call spans.
            assertTrue(attributes(b).toString().contains("SPANLESS-B-MARKER"), mode + ": " + b);
            assertFalse(b.toString().contains("test-secret-key"), mode + ": " + b);
            for (Map<String, Object> span : adk) assertFalse(span.toString().contains("SPANLESS-B-MARKER"), mode + ": B data in an ADK span: " + span);
        }
    }

    /** A span-less request that is traced is still redacted: a registered secret is absent from the exported bytes, from its body, query and header. */
    @Test void aSpanLessRequestThatIsTracedIsStillRedacted() throws Exception {
        String marker = "SPANLESS-MARKER-KEY-77310042";
        for (String mode : List.of("plain", "sdk")) {
            spans.clear(); exported.clear(); modelCalls.set(0);
            run(mode, PROMPT, Map.of("SPANLESS_MARKER_KEY", marker), "subscribe", "span-less");
            assertEquals(1, byLayer("wire").size(), mode + ": the span-less request is traced: " + spans);
            assertFalse(exported.isEmpty());
            for (String bytes : exported) assertFalse(bytes.contains(marker), mode + ": the marker reached the exporter: " + bytes);
            assertTrue(attributes(byLayer("wire").get(0)).toString().contains("SPANLESS-B-MARKER"), mode + ": the request's other content is kept");
        }
    }

    @Test void aFailedModelCallKeepsItsErrorTextWithSecretsReplacedWhenDetailsAreShown() throws Exception {
        run("plain", PROMPT, Map.of(), "subscribe", "error");
        Map<String, Object> llm = named("call_llm").get(0);
        String message = String.valueOf(((Map<?, ?>) llm.get("status")).get("message"));
        assertTrue(message.contains("SEED-MODEL-ERROR"), message);
        assertFalse(spans.toString().contains("test-secret-key"), spans.toString());
    }

    @Test void adkSpansDoNotDependOnTheWireSwitches() throws Exception {
        for (Map<String, String> options : List.of(Map.of("FI_WIRE_CAPTURE", "false"), Map.of("FI_DISABLED_INSTRUMENTATIONS", "wire"),
                Map.of("FI_INSTRUMENTATIONS", "google-adk"))) {
            spans.clear();
            String log = run("plain", PROMPT, options);
            assertEquals(5, spans.size(), options + ": ADK spans are still recorded: " + spans);
            assertEquals(5, byLayer("google-adk").size(), options + ": " + spans);
            assertTrue(log.lines().anyMatch(l -> l.startsWith("DOCTOR ") && l.contains("\"google-adk\":\"instrumented\"")), options + ": " + log);
        }
    }

    @Test void onlyWiringTheWireIntegrationTurnsGoogleAdkOffAndTheStatusSaysSo() throws Exception {
        String log = run("plain", PROMPT, Map.of("FI_INSTRUMENTATIONS", "wire"));
        assertEquals(List.of(), byLayer("google-adk"), spans.toString());
        assertTrue(log.lines().anyMatch(l -> l.startsWith("DOCTOR ") && l.contains("\"google-adk\":\"disabled\"")), log);
    }

    @Test void disablingGoogleAdkLeavesOnlyTheWireSpans() throws Exception {
        String log = run("plain", PROMPT, Map.of("FI_DISABLED_INSTRUMENTATIONS", "google-adk"));
        assertEquals(List.of(), byLayer("google-adk"), spans.toString());
        assertEquals(2, byLayer("wire").size(), "each HTTP call is a wire span again: " + spans);
        assertTrue(log.contains("\"google-adk\":\"disabled\""), log);
    }

    @Test void hideInputsAndOutputsKeepContentOutOfEverySpan() throws Exception {
        run("plain", PROMPT, Map.of("FI_HIDE_INPUTS", "true", "FI_HIDE_OUTPUTS", "true"));
        assertEquals(5, spans.size(), spans.toString());
        String everything = spans.toString();
        for (String content : List.of(PROMPT, ANSWER, TOOL_RESULT, "Kyoto")) assertFalse(everything.contains(content), content + " in " + everything);
        assertEquals("11", String.valueOf(attributes(named("call_llm").get(0)).get("gen_ai.usage.input_tokens")), "counts stay");
    }

    @Test void hideInputsAloneKeepsThePromptAndToolArgumentsOut() throws Exception {
        run("plain", PROMPT, Map.of("FI_HIDE_INPUTS", "true"));
        String everything = spans.toString();
        assertFalse(everything.contains(PROMPT), everything);
        for (Map<String, Object> span : spans) {
            assertFalse(attributes(span).containsKey("gen_ai.tool.call.arguments"), "tool arguments are input: " + span);
            assertFalse(attributes(span).keySet().stream().anyMatch(k -> k.startsWith("gen_ai.input.")), "input messages: " + span);
        }
        assertTrue(everything.contains(ANSWER), "outputs are still recorded: " + everything);
    }

    @Test void aKnownSecretInThePromptIsReplaced() throws Exception {
        run("plain", PROMPT + " key=test-secret-key", Map.of());
        assertFalse(spans.toString().contains("test-secret-key"), spans.toString());
        assertTrue(spans.toString().contains(PROMPT), spans.toString());
    }

    @Test void anApplicationWithItsOwnSdkKeepsItsSpansAndGetsTheAgentsToo() throws Exception {
        String log = run("sdk", PROMPT, Map.of());
        // The application's provider recorded ADK's own spans, unchanged and still linked.
        for (String name : List.of("invocation", "invoke_agent weather_agent", "call_llm", "execute_tool"))
            assertTrue(log.lines().anyMatch(l -> l.startsWith("OWN " + name + " ") || name.equals("execute_tool") && l.startsWith("OWN execute_tool ")), name + " in " + log);
        assertTrue(log.lines().filter(l -> l.startsWith("OWN ")).anyMatch(l -> l.contains("parent=true")), log);
        assertEquals(5, spans.size(), "the agent's own spans reach Future AGI as well: " + spans);
        assertEquals(List.of(), byLayer("wire"));
    }

    /** Beside the OpenTelemetry Java agent (found by its premain class on the class path) this agent stays off, and the doctor says so. */
    @Test void doctorBesideTheOpenTelemetryJavaAgentSaysGoogleAdkAndWireAreOffAndNothingIsExported() throws Exception {
        Path other = Files.createTempDirectory(Path.of("target"), "otel-agent-marker-");
        Path marker = other.resolve("io/opentelemetry/javaagent/OpenTelemetryAgent.class");
        Files.createDirectories(marker.getParent()); Files.write(marker, new byte[0]);
        extraClasspath = other;
        try {
            String log = run("plain", PROMPT, Map.of());
            String doctor = log.lines().filter(l -> l.startsWith("DOCTOR ")).findFirst().orElseThrow();
            assertTrue(doctor.contains("\"google-adk\":\"disabled (OpenTelemetry Java agent detected)\""), doctor);
            assertTrue(doctor.contains("\"wire\":\"disabled (OpenTelemetry Java agent detected)\""), doctor);
            assertEquals(List.of(), spans, "nothing is exported beside the other agent");
        } finally { extraClasspath = null; }
    }

    @Test void doctorListsTheIntegration() throws Exception {
        String log = run("plain", PROMPT, Map.of());
        assertTrue(log.lines().anyMatch(l -> l.startsWith("DOCTOR ") && l.contains("\"google-adk\":\"instrumented\"")), log);
    }

    // ---- ADK's own Gemini model client (google-genai) against a local stub of generateContent ---------------------------------

    /**
     * ADK's own Gemini client (AdkGeminiApp: Gemini built on google-genai's Client, with HttpOptions.baseUrl pointing at the loopback stub
     * and the API key given to the builder; no environment variable needed) is a real client path, so this is the real thing.
     *
     * <p>Findings by running it on ADK 1.15.0 (google-genai 1.77.0): see THREAD_FINDINGS below; the model's HTTP call is a single
     * request per model call, and the wire capture records no second span for it.
     */
    String runGemini(String mode, String prompt, Map<String, String> options) throws Exception {
        appClass = "ai.traceai.googleadk.AdkGeminiApp";
        try { return run(mode, prompt, options); } finally { appClass = "ai.traceai.googleadk.AdkApp"; }
    }

    @Test void adkOwnGeminiClientModelCallsAreOneSpanEachAndTheirHttpCallsAreNotTracedASecondTime() throws Exception {
        String log = runGemini("plain", PROMPT, Map.of());
        // Thread findings (ADK 1.15.0, google-genai 1.77.0, observed by AdkGeminiApp): ADK's Gemini is subscribed on the caller's thread and
        // blocks it for the answer (the answer is delivered on that same thread). google-genai's async client enqueues the OkHttp call
        // from that thread; OkHttp runs it on its own dispatcher thread ("OkHttp <url>", RealCall$AsyncCall.run), which carries no
        // OpenTelemetry context. The agent reads the context where the call is enqueued (the subscriber's thread, inside the call_llm
        // scope), so the hop does not matter: the HTTP call is not a second span.
        List<String> threads = log.lines().filter(l -> l.startsWith("THREAD-")).toList();
        assertTrue(threads.stream().anyMatch(l -> l.startsWith("THREAD-SUBSCRIBE main")), threads.toString());
        assertTrue(threads.contains("THREAD-HTTP main in RealCall.enqueue"), "the subscriber's thread enqueues the OkHttp call: " + threads);
        assertTrue(threads.contains("THREAD-HTTP OkHttp  in RealCall.getResponseWithInterceptorChain$okhttp"), "an OkHttp dispatcher thread runs it: " + threads);
        assertFalse(threads.contains("THREAD-HTTP main in RealCall.getResponseWithInterceptorChain$okhttp"), "not the subscriber's thread: " + threads);
        assertEquals(2, geminiCalls.get(), "the real Gemini client made its two HTTP calls: " + geminiRequests);
        assertEquals(0, modelCalls.get());
        for (String request : geminiRequests) assertTrue(request.startsWith("POST /v1beta/models/gemini-2.0-flash:generateContent"), request);
        Map<String, Object> invocation = only("invocation"), agent = only("invoke_agent weather_agent"), tool = tool();
        List<Map<String, Object>> llm = named("call_llm");
        assertEquals(2, llm.size(), "one model span per model call: " + spans);
        assertEquals(List.of(), byLayer("wire"), "the model's own HTTP call is not traced a second time: " + spans);
        assertEquals(5, spans.size(), spans.toString());
        assertNull(invocation.get("parentSpanId"));
        assertEquals(invocation.get("spanId"), agent.get("parentSpanId"));
        for (Map<String, Object> call : llm) assertEquals(agent.get("spanId"), call.get("parentSpanId"));
        assertTrue(llm.stream().anyMatch(s -> s.get("spanId").equals(tool.get("parentSpanId"))), "the tool runs under the call that asked for it");
        for (Map<String, Object> span : spans) assertEquals(invocation.get("traceId"), span.get("traceId"));
        Map<String, Object> second = llm.stream().map(GoogleAdkE2ETest::attributes).filter(x -> x.containsKey("gen_ai.input.messages.3.message.role")).findFirst().orElseThrow();
        assertEquals("LLM", second.get("gen_ai.span.kind"));
        assertEquals(PROMPT, second.get("gen_ai.input.messages.1.message.content"));
        assertEquals(ANSWER, second.get("gen_ai.output.messages.0.message.content"));
        assertEquals("11", String.valueOf(second.get("gen_ai.usage.input_tokens")));
        // The API key the client sent is a known secret: it is in no span and in no export.
        assertTrue(geminiRequests.get(0).contains("key=test-secret-key"), "control: the client sent the key: " + geminiRequests);
        assertFalse(spans.toString().contains("test-secret-key"), spans.toString());
        for (String bytes : exported) assertFalse(bytes.contains("test-secret-key"), bytes);
    }

    @Test void adkOwnGeminiClientHonoursTheHideSettingsAndReplacesKnownSecrets() throws Exception {
        runGemini("plain", PROMPT + " key=test-secret-key", Map.of());
        assertEquals(List.of(), byLayer("wire"), spans.toString());
        assertFalse(spans.toString().contains("test-secret-key"), spans.toString());
        assertTrue(spans.toString().contains(PROMPT), spans.toString());
        spans.clear(); geminiCalls.set(0);
        runGemini("plain", PROMPT, Map.of("FI_HIDE_INPUTS", "true", "FI_HIDE_OUTPUTS", "true"));
        assertEquals(2, geminiCalls.get());
        assertEquals(5, spans.size(), spans.toString());
        assertEquals(List.of(), byLayer("wire"), spans.toString());
        String everything = spans.toString();
        for (String content : List.of(PROMPT, ANSWER, TOOL_RESULT, "Kyoto", "test-secret-key")) assertFalse(everything.contains(content), content + " in " + everything);
        assertEquals("11", String.valueOf(attributes(named("call_llm").get(0)).get("gen_ai.usage.input_tokens")), "counts stay");
    }


    // ---- TracedLlm and TracedTool beside the agent ------------------------------------------------------------

    static final String SKIP = "FI_GOOGLE_ADK_SKIP_WRAPPED_CALLS";

    /** The "WRAP" or "OWNID" lines of the child's output: span name, id and parent id. */
    static List<String[]> idLines(String log, String prefix) {
        java.util.regex.Pattern pattern = java.util.regex.Pattern.compile("^" + prefix + " (.*) id=(\\w+) parent=(\\w*)(?: trace=(\\w+))?$");
        List<String[]> out = new ArrayList<>();
        for (String line : log.lines().toList()) {
            var m = pattern.matcher(line.trim());
            if (m.matches()) out.add(new String[]{m.group(1), m.group(2), m.group(3)});
        }
        return out;
    }
    static List<String[]> withName(List<String[]> lines, String prefix) { return lines.stream().filter(l -> l[0].startsWith(prefix)).toList(); }
    static Set<String> ids(List<String[]> lines) { return lines.stream().map(l -> l[1]).collect(Collectors.toSet()); }

    /** Each model and tool call goes through the wrappers; the application's own provider records ADK's spans. */
    @Test void withSkippingOffEachWrappedCallIsTheWrapperSpanAndTheAgentSpan() throws Exception {
        String log = run("sdk", PROMPT, Map.of(), "none", "wrapped");
        List<String[]> wrap = idLines(log, "WRAP");
        assertEquals(2, withName(wrap, "call_llm").size(), log);
        assertEquals(1, withName(wrap, "execute_tool").size(), log);
        assertEquals(2, named("call_llm").size(), "the agent's span for each model call: " + spans);
        assertEquals(1, named("execute_tool").size(), "the agent's span for the tool call: " + spans);
        assertEquals(5, spans.size(), spans.toString());
    }

    @Test void withSkippingOnEachWrappedCallIsOnlyTheWrapperSpanAndTheChainOfTheOtherSpansIsIntact() throws Exception {
        String log = run("sdk", PROMPT, Map.of(SKIP, "true"), "none", "wrapped");
        List<String[]> wrap = idLines(log, "WRAP"), own = idLines(log, "OWNID");
        // The wrapper's span for each call, with the parent it has always had: ADK's span the application's provider recorded.
        assertEquals(2, withName(wrap, "call_llm").size(), log);
        assertEquals(1, withName(wrap, "execute_tool").size(), log);
        for (String[] w : withName(wrap, "call_llm")) assertTrue(ids(withName(own, "call_llm")).contains(w[2]), "wrapper model span under ADK's model span: " + log);
        for (String[] w : withName(wrap, "execute_tool")) assertTrue(ids(own).contains(w[2]) || ids(withName(wrap, "call_llm")).contains(w[2]), "wrapper tool span under ADK's tool span, or (older ADK versions) under the model call that asked for it: " + log);
        // The agent adds no second span for them.
        assertEquals(List.of(), named("call_llm"), spans.toString());
        assertEquals(List.of(), named("execute_tool"), spans.toString());
        assertEquals(2, spans.size(), spans.toString());
        Map<String, Object> invocation = only("invocation"), agent = only("invoke_agent weather_agent");
        assertNull(invocation.get("parentSpanId"));
        assertEquals(invocation.get("spanId"), agent.get("parentSpanId"));
        assertEquals(invocation.get("traceId"), agent.get("traceId"));
        // The summary the skipped calls would have given their ancestors is kept.
        Map<String, Object> a = attributes(agent);
        assertEquals("user-1", a.get("user.id"));
        assertNotNull(a.get("session.id"));
        assertEquals(PROMPT, a.get("input.value"));
        assertEquals(ANSWER, a.get("output.value"));
        if (!adkVersion100) {
            Map<String, Object> top = attributes(invocation);
            assertEquals("user-1", top.get("user.id"));
            assertNotNull(top.get("session.id"));
            assertEquals(PROMPT, top.get("input.value"));
            assertEquals(ANSWER, top.get("output.value"));
        }
    }

    /** Only the wrapped model: the tool's span stays, under the nearest span the agent still records. */
    @Test void withSkippingOnAWrappedModelAloneTheToolSpanKeepsAnExportedParent() throws Exception {
        String log = run("sdk", PROMPT, Map.of(SKIP, "true"), "none", "wrapped-llm");
        assertEquals(2, withName(idLines(log, "WRAP"), "call_llm").size(), log);
        assertEquals(List.of(), named("call_llm"), spans.toString());
        Map<String, Object> tool = tool(), agent = only("invoke_agent weather_agent");
        assertEquals(agent.get("spanId"), tool.get("parentSpanId"), "no reference to a span that is not exported: " + spans);
        assertEquals(3, spans.size(), spans.toString());
        assertEquals(ANSWER, attributes(agent).get("output.value"));
        assertEquals(PROMPT, attributes(agent).get("input.value"));
    }

    @Test void withSkippingOnAWrappedToolAloneTheModelSpansStay() throws Exception {
        String log = run("sdk", PROMPT, Map.of(SKIP, "true"), "none", "wrapped-tool");
        assertEquals(1, withName(idLines(log, "WRAP"), "execute_tool").size(), log);
        assertEquals(List.of(), named("execute_tool"), spans.toString());
        assertEquals(2, named("call_llm").size(), spans.toString());
        assertEquals(4, spans.size(), spans.toString());
        Map<String, Object> agent = only("invoke_agent weather_agent");
        for (Map<String, Object> call : named("call_llm")) assertEquals(agent.get("spanId"), call.get("parentSpanId"));
    }

    /** No exported span refers to a parent that was not exported, and every parent is in the span's own trace. */
    void assertNoOrphans() {
        Map<String, Map<String, Object>> byId = new java.util.HashMap<>();
        for (Map<String, Object> s : spans) byId.put(String.valueOf(s.get("spanId")), s);
        for (Map<String, Object> s : spans) {
            Object parent = s.get("parentSpanId");
            if (parent == null || "".equals(parent)) continue;
            Map<String, Object> p = byId.get(String.valueOf(parent));
            assertNotNull(p, "the parent of " + s.get("name") + " is an exported span: " + spans);
            assertEquals(p.get("traceId"), s.get("traceId"), "the parent is in the same trace: " + spans);
        }
    }

    /** A wrapped tool that runs a sub-agent: the sub-agent's spans start while the tool runs, and the tool's own span is skipped. */
    @Test void withSkippingOnAWrappedAgentToolNoExportedSpanRefersToAnUnexportedOne() throws Exception {
        String log = run("sdk", PROMPT, Map.of(SKIP, "true"), "none", "wrapped-agent-tool");
        assertEquals(1, withName(idLines(log, "WRAP"), "execute_tool").size(), log);
        assertEquals(List.of(), named("execute_tool"), "the wrapped call is recorded once: " + spans);
        assertFalse(named("invoke_agent sub_agent").isEmpty(), "the sub-agent's own spans are recorded: " + spans);
        assertNoOrphans();
    }

    /** A wrapped tool whose work is subscribed to lazily and makes an HTTP call of its own. */
    @Test void withSkippingOnAWrappedDeferredToolNoExportedSpanRefersToAnUnexportedOne() throws Exception {
        String log = run("sdk", PROMPT, Map.of(SKIP, "true"), "none", "wrapped-deferred-tool");
        assertEquals(1, withName(idLines(log, "WRAP"), "execute_tool").size(), log);
        assertEquals(List.of(), named("execute_tool"), "the wrapped call is recorded once: " + spans);
        assertEquals(1, byLayer("wire").size(), "the tool's HTTP call is a wire span: " + spans);
        assertNoOrphans();
    }

    @Test void withSkippingOnCallsThatDoNotGoThroughTheWrappersAreStillRecorded() throws Exception {
        run("sdk", PROMPT, Map.of(SKIP, "true"), "none", "single");
        assertEquals(2, named("call_llm").size(), spans.toString());
        assertEquals(1, named("execute_tool").size(), spans.toString());
        assertEquals(5, spans.size(), spans.toString());
    }

    @Test void theSkipOptionIsInTheConfigurationTheDoctorPrintsAndIsOffByDefault() throws Exception {
        String off = run("plain", PROMPT, Map.of(), "none", "single");
        assertTrue(off.contains("\"" + SKIP + "\":\"false\""), off);
        String on = run("plain", PROMPT, Map.of(SKIP, "true"), "none", "single");
        assertTrue(on.contains("\"" + SKIP + "\":\"true\""), on);
    }
}
