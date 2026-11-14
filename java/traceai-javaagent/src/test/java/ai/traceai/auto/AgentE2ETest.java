package ai.traceai.auto;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.io.File;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.jupiter.api.Assertions.*;

class AgentE2ETest {
    HttpServer server;
    ExecutorService serverExecutor;
    volatile boolean redirect, authenticate, holdStreams, echoRetryCredentials;
    volatile String retryCredential;
    final List<Map<String, Object>> scopes = new CopyOnWriteArrayList<>();
    volatile CountDownLatch streamRelease;
    final java.util.concurrent.atomic.AtomicInteger uploads = new java.util.concurrent.atomic.AtomicInteger();
    final List<Map<String, Object>> spans = new CopyOnWriteArrayList<>();
    final List<Object> collectorPayloads = new CopyOnWriteArrayList<>();
    final List<Map<String, Object>> resources = new CopyOnWriteArrayList<>();
    /** Spans the existing wrappers' own exporter (TraceAI.init) sent, as OTLP protobuf. */
    final List<Map<String, Object>> wrapperSpans = new CopyOnWriteArrayList<>();
    volatile String sentRequest;
    final Map<String, String> receivedHeaders = new ConcurrentHashMap<>();
    volatile byte[] payload;
    volatile String type = "application/json", encoding;
    volatile int status = 200;
    /** When set, the child JVM runs on these OkHttp, Okio and Kotlin jars instead of the build's OkHttp 3 and Okio 1. */
    volatile List<Path> okHttpRuntime;
    static final String BASIC = "{\"id\":\"mock-id\",\"model\":\"test-model\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"Hello world\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";
    static final String STREAM = "data: {\"id\":\"mock-id\",\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hello \"}}]}\n\ndata: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"world\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n\ndata: [DONE]\n\n";
    /** The packaged agent jar; the build passes its versioned path to the integration tests. */
    static Path agentJar() {
        String packaged = System.getProperty("traceai.agent.jar");
        assertNotNull(packaged, "traceai.agent.jar is set by the failsafe configuration");
        return Path.of(packaged).toAbsolutePath();
    }
    @BeforeEach void startServer() throws Exception {
        payload = BASIC.getBytes(StandardCharsets.UTF_8);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        com.sun.net.httpserver.HttpHandler application = exchange -> {
            sentRequest = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8); uploads.incrementAndGet();
            exchange.getRequestHeaders().forEach((name, values) -> receivedHeaders.put(name.toLowerCase(Locale.ROOT), values.get(0)));
            if (redirect && exchange.getRequestURI().getQuery() == null) { exchange.getResponseHeaders().add("Location", "/v1/chat/completions?redirected=1"); exchange.sendResponseHeaders(307, -1); exchange.close(); return; }
            if (authenticate && exchange.getRequestHeaders().getFirst("Authorization") == null) { exchange.getResponseHeaders().add("WWW-Authenticate", "Basic realm=test"); exchange.sendResponseHeaders(401, -1); exchange.close(); return; }
            if (echoRetryCredentials) {
                retryCredential = exchange.getRequestHeaders().getFirst("Authorization").replaceFirst("^\\S+\\s+", "");
                status = 400; payload = WireCore.dump(Map.of("error", Map.of("message", "echo " + retryCredential))).getBytes(StandardCharsets.UTF_8);
            }
            CountDownLatch gate = holdStreams ? new CountDownLatch(1) : null; streamRelease = gate;
            exchange.getResponseHeaders().add("Content-Type", type);
            if (encoding != null) exchange.getResponseHeaders().add("Content-Encoding", encoding);
            exchange.sendResponseHeaders(status, 0);
            try (var out = exchange.getResponseBody()) {
                int middle = payload.length / 2; out.write(payload, 0, middle); out.flush();
                if (gate != null) try { if (!gate.await(10, TimeUnit.SECONDS)) throw new java.io.IOException("stream release timed out"); } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                out.write(payload, middle, payload.length - middle);
            }
            exchange.close();
        };
        server.createContext("/v1/", application); server.createContext("/api/", application);
        server.createContext("/tracer/v1/traces", exchange -> {
            byte[] body = exchange.getRequestBody().readAllBytes();
            if (String.valueOf(exchange.getRequestHeaders().getFirst("Content-Type")).contains("protobuf")) {
                var request = io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest.parseFrom(body);
                for (var resource : request.getResourceSpansList()) for (var scope : resource.getScopeSpansList()) for (var span : scope.getSpansList()) {
                    wrapperSpans.add(Map.of("name", span.getName(), "spanId", HexFormat.of().formatHex(span.getSpanId().toByteArray())));
                }
                exchange.getResponseHeaders().add("Content-Type", "application/x-protobuf"); exchange.sendResponseHeaders(200, -1); exchange.close(); return;
            }
            Object root = WireCore.parse(new String(body, StandardCharsets.UTF_8));
            collectorPayloads.add(root);
            for (Object resource : WireCore.list(WireCore.get(root, "resourceSpans"))) {
                resources.add(attributes(WireCore.map(WireCore.get(resource, "resource"))));
                for (Object scope : WireCore.list(WireCore.get(resource, "scopeSpans"))) {
                    scopes.add(WireCore.map(WireCore.get(scope, "scope")));
                    for (Object span : WireCore.list(WireCore.get(scope, "spans"))) spans.add(WireCore.map(span));
                }
            }
            exchange.getResponseHeaders().add("Content-Type", "application/json"); exchange.sendResponseHeaders(200, 2); exchange.getResponseBody().write("{}".getBytes(StandardCharsets.UTF_8)); exchange.close();
        });
        server.createContext("/release", exchange -> { CountDownLatch gate = streamRelease; if (gate != null) gate.countDown(); exchange.sendResponseHeaders(200, -1); exchange.close(); });
        serverExecutor = Executors.newFixedThreadPool(3); server.setExecutor(serverExecutor); server.start();
    }
    @AfterEach void stopServer() { server.stop(0); serverExecutor.shutdownNow(); }
    String run(String client, String scenario, Map<String, String> options, boolean credentials) throws Exception {
        Path agent = agentJar(); assertTrue(Files.isRegularFile(agent));
        String cp = System.getProperty("surefire.test.class.path", System.getProperty("java.class.path"));
        String classes = Path.of("target", "classes").toAbsolutePath().normalize().toString();
        cp = String.join(File.pathSeparator, Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator))).filter(p -> !Path.of(p).toAbsolutePath().normalize().toString().equals(classes)).toList());
        if (scenario.equals("legacy_okhttp")) {
            Path legacy = Path.of("target", "legacy-okhttp", "okhttp-3.8.1.jar").toAbsolutePath(); assertTrue(Files.isRegularFile(legacy));
            cp = legacy + File.pathSeparator + String.join(File.pathSeparator, Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
                .filter(p -> !Path.of(p).getFileName().toString().matches("okhttp-[0-9].*\\.jar")).toList());
        }
        if (okHttpRuntime != null) {
            // The application's own OkHttp, Okio and Kotlin jars replace every copy the build put on the classpath.
            for (Path jar : okHttpRuntime) assertTrue(Files.isRegularFile(jar), "the build copies " + jar);
            cp = String.join(File.pathSeparator, okHttpRuntime.stream().map(Path::toString).toList()) + File.pathSeparator + String.join(File.pathSeparator, Arrays.stream(cp.split(java.util.regex.Pattern.quote(File.pathSeparator)))
                .filter(p -> !Path.of(p).getFileName().toString().matches("(okhttp|okhttp-jvm|okio|okio-jvm|kotlin-stdlib|kotlin-stdlib-jdk7|kotlin-stdlib-jdk8|kotlin-stdlib-common)-[0-9].*\\.jar")).toList());
        }
        Path output = Files.createTempFile(Path.of("target"), "agent-e2e-", ".log");
        String path = scenario.equals("audio") ? "/v1/audio/speech" : scenario.startsWith("multipart") ? "/v1/audio/transcriptions" : scenario.equals("metadata_ordinary") ? "/v1/ordinary" : "/v1/chat/completions";
        String endpoint = "http://127.0.0.1:" + server.getAddress().getPort() + path;
        List<String> command = new ArrayList<>();
        command.add(Path.of(System.getProperty("traceai.test.java.home", System.getProperty("java.home")), "bin", "java").toString());
        String otelAgent = "-javaagent:" + Path.of("target", "otel-javaagent", "opentelemetry-javaagent.jar").toAbsolutePath();
        if ("first".equals(options.get("TRACEAI_TEST_OTEL_AGENT"))) command.add(otelAgent);
        if (!client.equals("init") && !scenario.equals("coexist_existing") && !options.containsKey("TRACEAI_TEST_NO_AGENT")) command.add("-javaagent:" + agent);
        if ("second".equals(options.get("TRACEAI_TEST_OTEL_AGENT"))) command.add(otelAgent);
        if (client.equals("jdkonly")) {
            Path app = Files.createTempFile(Path.of("target"), "plain-app-", ".jar");
            java.util.jar.Manifest manifest = new java.util.jar.Manifest(); manifest.getMainAttributes().putValue("Manifest-Version", "1.0"); manifest.getMainAttributes().putValue("Main-Class", JdkOnlyApp.class.getName());
            try (var jar = new java.util.jar.JarOutputStream(Files.newOutputStream(app), manifest)) {
                String entry = JdkOnlyApp.class.getName().replace('.', '/') + ".class"; jar.putNextEntry(new java.util.jar.JarEntry(entry));
                try (var in = getClass().getClassLoader().getResourceAsStream(entry)) { jar.write(Objects.requireNonNull(in).readAllBytes()); } jar.closeEntry();
            }
            command.addAll(List.of("-jar", app.toAbsolutePath().toString(), endpoint));
        } else if (client.equals("init")) {
            command.addAll(List.of("-cp", agent + File.pathSeparator + Path.of("target", "test-classes").toAbsolutePath(), OneLineApp.class.getName(), endpoint));
        } else command.addAll(List.of("-cp", scenario.equals("coexist_existing") ? agent + File.pathSeparator + cp : cp, (scenario.startsWith("okhttp_version") ? OkHttpVersionApp.class : PlainApp.class).getName(), client, endpoint, scenario));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        Map<String, String> env = pb.environment(); env.keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        env.put("FI_CONFIG_FILE", "target/no-e2e-config.toml"); env.put("FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort());
        if (credentials) { env.put("FI_API_KEY", "test-api-key"); env.put("FI_SECRET_KEY", "test-secret-key"); }
        env.putAll(options);
        if (scenario.equals("parity_unread")) env.put("FI_AUTO_DEBUG", "true");
        Process process = pb.start();
        if (!process.waitFor(25, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("Child timed out: " + Files.readString(output)); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log);
        if (scenario.equals("verify_abort") || scenario.equals("verify_eager")) assertTrue(log.contains("ABORT_ORIGINAL=true"), log);
        else assertTrue(log.contains("BODY=" + Base64.getEncoder().encodeToString(scenario.startsWith("verify_gzip") ? STREAM.getBytes(StandardCharsets.UTF_8) : payload)), log);
        if (!scenario.equals("earlyreject")) assertEquals(PlainApp.request(scenario), sentRequest, "application request must be unchanged");
        return log;
    }
    Map<String, Object> attributes(Map<String, Object> span) {
        Map<String, Object> attrs = new LinkedHashMap<>();
        for (Object raw : WireCore.list(span.get("attributes"))) {
            Map<String, Object> value = WireCore.map(WireCore.get(raw, "value")); Object parsed = WireCore.or(value.get("stringValue"), value.get("intValue"), value.get("boolValue"), value.get("doubleValue"), value.get("arrayValue"));
            attrs.put(WireCore.str(WireCore.get(raw, "key")), parsed);
        }
        return attrs;
    }
    static String line(String log, String prefix) {
        return log.lines().filter(l -> l.startsWith(prefix)).findFirst().orElseThrow(() -> new AssertionError(prefix + " missing: " + log)).substring(prefix.length());
    }
    void assertSpan() {
        assertEquals(1, spans.size(), spans.toString()); assertWireSpan(spans.get(0));
    }
    void assertWireSpan(Map<String, Object> span) {
        Map<String, Object> attrs = attributes(span);
        assertEquals("chat test-model", span.get("name")); assertEquals(3, ((Number)span.get("kind")).intValue());
        assertEquals("wire", attrs.get("traceai.capture.layer")); assertEquals("openai_compatible", attrs.get("gen_ai.provider.name")); assertEquals("Hello world", attrs.get("output.value"));
        assertEquals("3", attrs.get("gen_ai.usage.input_tokens")); assertEquals("2", attrs.get("gen_ai.usage.output_tokens"));
        assertEquals(1, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        assertTrue(Long.parseLong(String.valueOf(span.get("startTimeUnixNano"))) <= Long.parseLong(String.valueOf(span.get("endTimeUnixNano"))));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void lateApplicationResourceIdentityAndBehaviourMatchNoAgent(String client) throws Exception {
        String baseline = run(client, "resource_identity", Map.of("TRACEAI_TEST_NO_AGENT", "1", "FI_PROJECT_NAME", "routing-only", "OTEL_SERVICE_NAME", "wrong-fallback"), true);
        assertTrue(spans.isEmpty());
        String traced = run(client, "resource_identity", Map.of("FI_PROJECT_NAME", "routing-only", "OTEL_SERVICE_NAME", "wrong-fallback"), true);
        assertEquals(baseline.lines().filter(l -> l.startsWith("BODY=") || l.startsWith("HOST_RESOURCE=")).toList(), traced.lines().filter(l -> l.startsWith("BODY=") || l.startsWith("HOST_RESOURCE=")).toList());
        // Only the wire span is exported; the application's CHAIN span stays with its own provider.
        assertEquals(1, spans.size(), spans.toString()); assertWireSpan(spans.get(0)); assertEquals(2, uploads.get());
        for (var resource : resources) {
            assertEquals("resource-app-service", resource.get("service.name")); assertEquals("resource-app-space", resource.get("service.namespace"));
            assertEquals("resource-app-instance", resource.get("service.instance.id")); assertEquals("application-owned", resource.get("custom.identity"));
            assertEquals("routing-only", resource.get("project_name"));
        }
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void plainAppProducesSpan(String client) throws Exception { run(client, "basic", Map.of(), true); assertSpan(); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void streamingIsByteForByteUnchanged(String client) throws Exception {
        payload = STREAM.getBytes(StandardCharsets.UTF_8); type = "text/event-stream"; run(client, "stream", Map.of(), true); assertSpan(); assertEquals(true, attributes(spans.get(0)).get("gen_ai.request.stream")); assertTrue(attributes(spans.get(0)).containsKey("gen_ai.response.time_to_first_chunk"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void asynchronousStreaming(String client) throws Exception { payload = STREAM.getBytes(StandardCharsets.UTF_8); type = "text/event-stream"; run(client, "async", Map.of(), true); assertSpan(); }
    /** The application's LLM span and the call under it stay local; the control call after the span is traced. */
    void assertLibrarySpanStaysLocalAndOnlyTheControlCallIsTraced(String log) {
        assertEquals(1, spans.size(), spans.toString()); assertWireSpan(spans.get(0));
        assertNull(spans.get(0).get("parentSpanId"), "the control call runs outside every application span");
        assertNotEquals(line(log, "PARENT="), spans.get(0).get("parentSpanId"));
        assertFalse(WireCore.dump(collectorPayloads).contains("application-parent")); assertEquals(2, uploads.get());
    }
    void assertFrameworkParent(String log) {
        // Only the wire span is exported. Its parent is the application's CHAIN span, which stays with the application.
        assertEquals(1, spans.size(), spans.toString());
        var wire = spans.get(0); assertWireSpan(wire);
        assertEquals(line(log, "PARENT="), wire.get("parentSpanId")); assertFalse(WireCore.dump(collectorPayloads).contains("application-parent"));
        // The session id set through ContextAttributes.usingSession is exported as set, under both of its keys.
        assertEquals("e2e-session", attributes(wire).get("session.id")); assertEquals("e2e-session", attributes(wire).get("gen_ai.conversation.id"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void librarySpanSuppressesWire(String client) throws Exception { assertLibrarySpanStaysLocalAndOnlyTheControlCallIsTraced(run(client, "dedup", Map.of(), true)); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void frameworkIsParentAndContextIsCaptured(String client) throws Exception {
        assertFrameworkParent(run(client, "framework", Map.of(), true));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void missingCredentialsFailOpenWithOneWarning(String client) throws Exception { String log = run(client, "basic", Map.of(), false); assertTrue(spans.isEmpty()); assertEquals(1, log.lines().filter(l -> l.contains("[traceai-auto]")).count(), log); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void asynchronousLibrarySpanSuppressesWire(String client) throws Exception { assertLibrarySpanStaysLocalAndOnlyTheControlCallIsTraced(run(client, "dedup_async", Map.of(), true)); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void asynchronousFrameworkContextSurvivesExecutorHandoff(String client) throws Exception {
        assertFrameworkParent(run(client, "framework_async", Map.of(), true));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void initWithKeysStartsAfterAKeylessJavaagentStart(String client) throws Exception {
        String log = run(client, "late_keys", Map.of(), false);
        assertSpan(); assertEquals(1, log.lines().filter(l -> l.contains("FI_API_KEY and FI_SECRET_KEY are required")).count(), log);
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({"first,jdk", "second,jdk", "first,okhttp", "second,okhttp"})
    void openTelemetryJavaAgentBesideThisOneKeepsItOffWithOneWarning(String order, String client) throws Exception {
        assertTrue(Files.isRegularFile(Path.of("target", "otel-javaagent", "opentelemetry-javaagent.jar")), "the build copies the OpenTelemetry Java agent");
        // The OkHttp application builds its own client on the build's OkHttp and reports the interceptors this agent added to it.
        String log = run(client, client.equals("okhttp") ? "okhttp_version" : "basic", Map.of("TRACEAI_TEST_OTEL_AGENT", order, "OTEL_TRACES_EXPORTER", "none", "OTEL_METRICS_EXPORTER", "none", "OTEL_LOGS_EXPORTER", "none"), true);
        // The application runs unchanged (run() compares its request and response), and this agent exports nothing.
        assertTrue(spans.isEmpty(), spans.toString()); assertTrue(collectorPayloads.isEmpty());
        var warnings = log.lines().filter(l -> l.contains("[traceai-auto]")).toList();
        assertEquals(1, warnings.size(), log); assertTrue(warnings.get(0).contains("OpenTelemetry Java agent"), log);
        // Seeing no span is not enough for OkHttp: the client itself must be left without this agent's interceptors.
        if (client.equals("okhttp")) assertEquals("0", line(log, "AGENT_INTERCEPTORS="), log);
    }
    @Test void packagedJarHoldsOnlyTheAgentsOwnPackagesAndMetadata() throws Exception {
        // Everything bundled is relocated under ai/traceai/auto/, so nothing can clash with the application's classpath.
        try (var jar = new java.util.jar.JarFile(agentJar().toFile())) {
            var foreign = jar.stream().map(java.util.zip.ZipEntry::getName)
                .filter(name -> !name.startsWith("ai/traceai/auto/") && !name.startsWith("META-INF/") && !name.equals("ai/") && !name.equals("ai/traceai/")).toList();
            assertEquals(List.of(), foreign);
        }
    }
    @Test void deadCollectorDoesNotAffectApplication() throws Exception { String log = run("jdk", "basic", Map.of("FI_BASE_URL", "http://127.0.0.1:1"), true); assertTrue(spans.isEmpty()); assertEquals(1, log.lines().filter(l -> l.contains("[traceai-auto]")).count(), log); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void individualTransportCanBeDisabled(String client) throws Exception { run(client, "basic", Map.of("FI_DISABLED_INSTRUMENTATIONS", client.equals("jdk") ? "java.net.http" : "okhttp"), true); assertTrue(spans.isEmpty()); }
    @Test void disabledWireDoesNotAffectApplication() throws Exception { run("okhttp", "basic", Map.of("FI_DISABLED_INSTRUMENTATIONS", "wire"), true); assertTrue(spans.isEmpty()); }
    @Test void selfContainedAgentWithExecutableAppJar() throws Exception { run("jdkonly", "basic", Map.of(), true); assertSpan(); }
    @Test void oneLineInitializationAttachesToRunningVm() throws Exception { run("init", "basic", Map.of(), true); assertSpan(); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void suppressionScopePreventsCapture(String client) throws Exception { run(client, "suppressed", Map.of(), true); assertTrue(spans.isEmpty()); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void binaryAudioIsUnchangedAndCountedOnly(String client) throws Exception {
        type = "audio/wav"; payload = new byte[12000]; Arrays.fill(payload, (byte)17);
        run(client, "audio", Map.of(), true); assertEquals(1, spans.size()); Map<String,Object> attrs = attributes(spans.get(0));
        assertEquals("text_to_speech tts-1", spans.get(0).get("name")); assertEquals("12000", attrs.get("traceai.audio.output_bytes")); assertEquals("Speak", attrs.get("input.value")); assertFalse(attrs.containsKey("output.value"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void multipartAudioRecordsOnlyMetadata(String client) throws Exception {
        payload = "{\"text\":\"Transcribed\",\"duration\":2.5}".getBytes(StandardCharsets.UTF_8);
        run(client, "multipart", Map.of(), true); assertEquals(1, spans.size()); Map<String,Object> attrs = attributes(spans.get(0));
        assertEquals("transcription whisper-1", spans.get(0).get("name")); assertEquals("102400", attrs.get("traceai.audio.input_bytes")); assertEquals("Transcribed", attrs.get("output.value")); assertFalse(String.valueOf(attrs.get("input.value")).contains("\\u0000"));
    }
    @Test void doctorIsMaskedAndDoesNotExportUnlessRequested() throws Exception { runDoctor(false); assertTrue(spans.isEmpty()); }
    @Test void doctorCanExplicitlyTestCollector() throws Exception { runDoctor(true); assertEquals(1, spans.size()); assertEquals("traceai.auto.doctor", spans.get(0).get("name")); }
    @ParameterizedTest @ValueSource(booleans = {false, true}) void collectorUrlDiagnosticsScrubFiKeysInDoctorAndDebug(boolean premain) throws Exception {
        Path stdout = Files.createTempFile(Path.of("target"), "doctor-url-out-", ".log"), stderr = Files.createTempFile(Path.of("target"), "doctor-url-err-", ".log");
        String api = "S6_SYNTHETIC_FI_API_81973264", secret = "S6_SYNTHETIC_FI_SECRET_81973264";
        String safeUrl = "http://127.0.0.1:" + server.getAddress().getPort() + "/public/<redacted>/<redacted>";
        Path agent = agentJar();
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString()));
        if (premain) command.add("-javaagent:" + agent);
        command.addAll(List.of("-jar", agent.toString(), "doctor"));
        ProcessBuilder pb = new ProcessBuilder(command).redirectOutput(stdout.toFile()).redirectError(stderr.toFile());
        pb.environment().keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        pb.environment().putAll(Map.of("FI_API_KEY", api, "FI_SECRET_KEY", secret, "FI_BASE_URL", "http://USERINFO_PRIVATE:USERINFO_PASSWORD@127.0.0.1:" + server.getAddress().getPort() + "/public/" + api + "/" + secret + "?token=QUERY_PRIVATE#FRAGMENT_PRIVATE", "FI_CONFIG_FILE", "target/nonexistent-doctor-config", "FI_AUTO_DEBUG", "true"));
        Process process = pb.start();
        try {
            if (!process.waitFor(20, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("doctor URL regression timed out"); }
            String output = Files.readString(stdout), debug = Files.readString(stderr);
            assertEquals(0, process.exitValue(), debug);
            for (String value : List.of(api, secret, "USERINFO_PRIVATE", "USERINFO_PASSWORD", "QUERY_PRIVATE", "FRAGMENT_PRIVATE")) {
                assertFalse(output.contains(value), "doctor stdout leaked " + value); assertFalse(debug.contains(value), "initialization stderr leaked " + value);
            }
            assertEquals(safeUrl, WireCore.get(WireCore.get(WireCore.parse(output), "config"), "FI_BASE_URL"));
            if (premain) {
                Object report = WireCore.parse(debug.lines().filter(line -> line.startsWith("{")).findFirst().orElseThrow());
                assertEquals(safeUrl, WireCore.get(WireCore.get(report, "config"), "FI_BASE_URL")); assertEquals(true, WireCore.get(report, "active"));
            }
            assertTrue(spans.isEmpty()); assertTrue(collectorPayloads.isEmpty(), "doctor without --check must not export");
        } finally { process.destroyForcibly(); Files.deleteIfExists(stdout); Files.deleteIfExists(stderr); }
    }
    void runDoctor(boolean check) throws Exception {
        Path output = Files.createTempFile(Path.of("target"), "doctor-", ".log");
        List<String> command = new ArrayList<>(List.of(Path.of(System.getProperty("java.home"), "bin", "java").toString()));
        Path agent = agentJar();
        if (check) command.add("-javaagent:" + agent);
        command.addAll(List.of("-jar", agent.toString(), "doctor")); if (check) command.add("--check");
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(output.toFile());
        pb.environment().keySet().removeIf(k -> k.startsWith("FI_") || k.startsWith("OTEL_"));
        pb.environment().putAll(Map.of("FI_API_KEY", "test-api-key", "FI_SECRET_KEY", "test-secret-key", "FI_BASE_URL", "http://127.0.0.1:" + server.getAddress().getPort(), "FI_CONFIG_FILE", "target/nonexistent-doctor-config"));
        Process process = pb.start(); if (!process.waitFor(20, TimeUnit.SECONDS)) { process.destroyForcibly(); fail("doctor timed out"); }
        String log = Files.readString(output); assertEquals(0, process.exitValue(), log); assertFalse(log.contains("test-api-key")); assertFalse(log.contains("test-secret-key"));
        Object report = WireCore.parse(log.lines().filter(l -> l.startsWith("{")).findFirst().orElseThrow());
        assertEquals("<masked>", WireCore.get(WireCore.get(report, "config"), "FI_API_KEY")); assertEquals(check ? "accepted" : "not_tested (agent inactive)", WireCore.get(report, "collector"));
        assertTrue(WireCore.map(WireCore.get(report, "integrations")).containsKey("spring-ai"));
    }
    @Test void disabledSdkProducesNoSpan() throws Exception { run("jdk", "basic", Map.of("OTEL_SDK_DISABLED", "true"), true); assertTrue(spans.isEmpty()); }
    @Test void samplingZeroProducesNoSpan() throws Exception { run("jdk", "basic", Map.of("FI_SAMPLE_RATE", "0"), true); assertTrue(spans.isEmpty()); }
    @Test void hideContent() throws Exception { run("jdk", "basic", Map.of("FI_HIDE_INPUTS", "true", "FI_HIDE_OUTPUTS", "true"), true); assertEquals(1, spans.size()); Map<String,Object> attrs = attributes(spans.get(0)); assertFalse(attrs.containsKey("input.value")); assertFalse(attrs.containsKey("output.value")); assertFalse(attrs.containsKey("gen_ai.input.messages.0.message.content")); }
    /** A hide switch with a value the agent does not recognise hides that content and says so once, by name and never by value. */
    @Test void unrecognisedHideSwitchHidesContentAndWarnsOnce() throws Exception {
        String log = run("jdk", "basic", Map.of("FI_HIDE_INPUTS", "maybe"), true);
        assertEquals(1, spans.size(), spans.toString()); Map<String,Object> attrs = attributes(spans.get(0));
        assertFalse(attrs.containsKey("input.value")); assertFalse(attrs.containsKey("gen_ai.input.messages.0.message.content")); assertEquals("Hello world", attrs.get("output.value"));
        List<String> warnings = log.lines().filter(line -> line.contains("FI_HIDE_INPUTS")).toList();
        assertEquals(1, warnings.size(), log); assertFalse(warnings.get(0).contains("maybe"), log);
        assertTrue(log.lines().noneMatch(line -> line.contains("[traceai-auto]") && line.contains("maybe")), log);
        // The warning lists the values the switch accepts.
        assertTrue(warnings.get(0).contains("true, 1, yes or on") && warnings.get(0).contains("false, 0, no or off"), log);
    }
    /** The configuration is resolved twice here, at a start without keys and again at init with keys; the warning is still printed once. */
    @Test void unrecognisedHideSwitchWarnsOnceWhenTheConfigurationIsResolvedTwice() throws Exception {
        String log = run("jdk", "late_keys", Map.of("FI_HIDE_INPUTS", "maybe"), false);
        assertEquals(1, spans.size(), spans.toString()); assertFalse(attributes(spans.get(0)).containsKey("input.value"));
        assertEquals(1, log.lines().filter(line -> line.contains("FI_HIDE_INPUTS")).count(), log);
    }
    /** A hide setting written as a single-quoted string in futureagi.toml is read as its text: 'on' hides and 'off' shows, with no warning. */
    @ParameterizedTest @ValueSource(strings = {"on", "off"})
    void singleQuotedHideSettingInTheConfigFileIsReadWithoutAWarning(String value) throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-e2e-", ".toml");
        try {
            Files.writeString(file, "hide_inputs = '" + value + "'\n");
            String log = run("jdk", "basic", Map.of("FI_CONFIG_FILE", file.toAbsolutePath().toString()), true);
            assertEquals(1, spans.size(), spans.toString()); Map<String,Object> attrs = attributes(spans.get(0));
            assertEquals(value.equals("off"), attrs.containsKey("input.value"), attrs.toString());
            assertEquals("Hello world", attrs.get("output.value"));
            assertTrue(log.lines().noneMatch(line -> line.contains("FI_HIDE_INPUTS")), log);
        } finally { Files.deleteIfExists(file); }
    }
    /**
     * A config file that is there but is not valid UTF-8 is reported once per JVM, by its path and never by its content.
     * The configuration is resolved twice here, at a start without keys and again at init with keys. The file's settings do not apply.
     */
    @Test void configFileThatIsNotValidUtf8IsReportedOnceWhenTheConfigurationIsResolvedTwice() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "not-utf8-e2e-", ".toml");
        try {
            // Saved as Latin-1: the last letter of the comment is one byte that is not valid UTF-8.
            Files.write(file, "hide_inputs = true # PRIVATE_NOTE café\n".getBytes(StandardCharsets.ISO_8859_1));
            String path = file.toAbsolutePath().toString();
            String log = run("jdk", "late_keys", Map.of("FI_CONFIG_FILE", path), false);
            assertSpan(); assertTrue(attributes(spans.get(0)).containsKey("input.value"), "the file's hide setting does not apply");
            List<String> warnings = log.lines().filter(line -> line.contains("] Config file ")).toList();
            assertEquals(1, warnings.size(), log);
            assertTrue(warnings.get(0).contains("not valid UTF-8") && warnings.get(0).contains("none of its settings apply"), log);
            assertFalse(log.contains("PRIVATE_NOTE"), log);
        } finally { Files.deleteIfExists(file); }
    }
    /** A one-line triple-quoted string on a hide key is not decoded: the content is hidden, even for "off", and the file says so once. */
    @Test void oneLineTripleQuotedHideSettingInTheConfigFileHidesAndWarnsOnce() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-switch-triple-e2e-", ".toml");
        try {
            Files.writeString(file, "hide_inputs = \"\"\"off\"\"\"\n");
            String log = run("jdk", "basic", Map.of("FI_CONFIG_FILE", file.toAbsolutePath().toString()), true);
            assertEquals(1, spans.size(), spans.toString()); Map<String,Object> attrs = attributes(spans.get(0));
            assertFalse(attrs.containsKey("input.value"), attrs.toString()); assertEquals("Hello world", attrs.get("output.value"));
            List<String> warnings = log.lines().filter(line -> line.contains("FI_HIDE_INPUTS")).toList();
            assertEquals(1, warnings.size(), log);
        } finally { Files.deleteIfExists(file); }
    }
    /** The provider's error text, in every place the agent writes it. */
    static final String PRIVATE_ERROR = "PRIVATE_PROVIDER_ERROR_TEXT";
    void assertErrorTextWithheld(String log) {
        assertEquals(1, spans.size(), spans.toString()); Map<String,Object> span = spans.get(0), attrs = attributes(span);
        assertEquals("401", attrs.get("error.type"));
        assertEquals("", WireCore.get(span.get("status"), "message"), "the status description is withheld");
        for (String key : List.of("exception.message", "exception.stacktrace", "error.message", "error.stack")) assertFalse(attrs.containsKey(key), key);
        assertFalse(WireCore.dump(collectorPayloads).contains(PRIVATE_ERROR), "no part of the exported span carries the error text");
        // Only the error text is withheld: the request is still captured.
        assertTrue(attrs.containsKey("input.value"), attrs.toString());
    }
    /** With FI_HIDE_ERROR_DETAILS as the only hide switch, the error text never reaches Future AGI. An unknown value hides it and warns once. */
    @ParameterizedTest @ValueSource(strings = {"true", "on", "maybe"})
    void errorDetailsSwitchWithholdsTheProviderErrorText(String value) throws Exception {
        status = 401; payload = ("{\"error\":{\"message\":\"" + PRIVATE_ERROR + "\"}}").getBytes(StandardCharsets.UTF_8);
        String log = run("jdk", "basic", Map.of("FI_HIDE_ERROR_DETAILS", value), true);
        assertErrorTextWithheld(log);
        List<String> warnings = log.lines().filter(line -> line.contains("FI_HIDE_ERROR_DETAILS")).toList();
        assertEquals(value.equals("maybe") ? 1 : 0, warnings.size(), log);
        assertTrue(log.lines().noneMatch(line -> line.contains("[traceai-auto]") && line.contains("maybe")), log);
    }
    /** A content switch alone, with no error switch, also keeps the provider's error text from the status description and the payload. */
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_OUTPUT_TEXT"})
    void aContentSwitchAloneWithholdsTheProviderErrorText(String name) throws Exception {
        status = 401; payload = ("{\"error\":{\"message\":\"" + PRIVATE_ERROR + "\"}}").getBytes(StandardCharsets.UTF_8);
        run("jdk", "basic", Map.of(name, "true"), true);
        assertEquals(1, spans.size(), spans.toString()); Map<String,Object> span = spans.get(0);
        assertEquals("401", attributes(span).get("error.type"));
        assertEquals("", WireCore.get(span.get("status"), "message"), name + ": the status description is withheld");
        assertFalse(WireCore.dump(collectorPayloads).contains(PRIVATE_ERROR), name + ": no part of the exported span carries the error text");
    }
    /**
     * A stream that fails partway, with only FI_HIDE_ERROR_DETAILS on: output.value and output.mime_type of the failed call are
     * dropped. The partial output messages stay under the output switches, so they are still sent.
     */
    @Test void errorDetailsSwitchDropsTheOutputValueOfAFailedCallButNotItsPartialMessages() throws Exception {
        payload = (TransportSafetyTest.chunk("partial")).getBytes(StandardCharsets.UTF_8);
        try (var socket = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress())) {
            socket.setSoTimeout(15000); var worker = Executors.newSingleThreadExecutor();
            try {
                var served = worker.submit(() -> {
                    try (var connection = socket.accept()) {
                        connection.setSoTimeout(5000); var input = connection.getInputStream(); var head = new java.io.ByteArrayOutputStream(); int tail = 0;
                        while (head.size() < 32768 && tail != 0x0d0a0d0a) { int b = input.read(); if (b < 0) throw new java.io.EOFException(); head.write(b); tail = tail << 8 | b; }
                        var length = java.util.regex.Pattern.compile("(?im)^Content-Length: *([0-9]+)").matcher(head.toString(StandardCharsets.US_ASCII));
                        if (!length.find()) throw new AssertionError("Missing request length");
                        sentRequest = new String(input.readNBytes(Integer.parseInt(length.group(1))), StandardCharsets.UTF_8);
                        var output = connection.getOutputStream(); output.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: " + (payload.length + 100) + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII)); output.write(payload); output.flush();
                    }
                    return true;
                });
                run("okhttp", "verify_transport", Map.of("FI_HIDE_ERROR_DETAILS", "true", "TRACEAI_TEST_PROXY_PORT", String.valueOf(socket.getLocalPort())), true);
                assertTrue(served.get(2, TimeUnit.SECONDS)); assertEquals(1, spans.size());
                var span = spans.get(0); Map<String,Object> attrs = attributes(span);
                assertEquals(2, ((Number)WireCore.get(span.get("status"), "code")).intValue(), "the call failed");
                assertFalse(attrs.containsKey("output.value"), attrs.toString());
                assertFalse(attrs.containsKey("output.mime_type"), attrs.toString());
                assertEquals("partial", attrs.get("gen_ai.output.messages.0.message.content"), "partial output messages stay under the output switches");
                assertTrue(attrs.containsKey("input.value"), "the request is still captured");
            } finally { worker.shutdownNow(); }
        }
    }
    @Test void errorDetailsSwitchOffLetsTheProviderErrorTextReachFutureAgi() throws Exception {
        status = 401; payload = ("{\"error\":{\"message\":\"" + PRIVATE_ERROR + "\"}}").getBytes(StandardCharsets.UTF_8);
        run("jdk", "basic", Map.of("FI_HIDE_ERROR_DETAILS", "false"), true);
        assertEquals(1, spans.size(), spans.toString());
        assertEquals(PRIVATE_ERROR, WireCore.get(spans.get(0).get("status"), "message"));
    }
    @Test void errorDetailsSwitchWorksFromTheConfigFileToo() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "hide-error-details-e2e-", ".toml");
        try {
            Files.writeString(file, "hide_error_details = true\n");
            status = 401; payload = ("{\"error\":{\"message\":\"" + PRIVATE_ERROR + "\"}}").getBytes(StandardCharsets.UTF_8);
            assertErrorTextWithheld(run("jdk", "basic", Map.of("FI_CONFIG_FILE", file.toAbsolutePath().toString()), true));
        } finally { Files.deleteIfExists(file); }
    }
    /**
     * The agent's AI spans are sampled by trace id at FI_SAMPLE_RATE. The application's own sampling decision does not
     * drop them: here the application's SDK samples nothing, and the call under its span still reaches Future AGI as
     * a child of that span, while the application's own span stays unsampled and never leaves the application.
     */
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void aCallUnderAnApplicationSpanThatTheApplicationDoesNotSampleIsStillTraced(String client) throws Exception {
        String log = run(client, "unsampled_app", Map.of(), true);
        assertTrue(log.contains("APP_SPAN_SAMPLED=false"), log); assertTrue(log.contains("APP_SPAN_STAYED_UNSAMPLED=true"), log);
        assertSpan(); assertEquals(line(log, "APP_SPAN="), spans.get(0).get("parentSpanId"));
        assertFalse(WireCore.dump(collectorPayloads).contains("application-unsampled-span"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void sampleRateZeroDropsEvenACallUnderAnUnsampledApplicationSpan(String client) throws Exception {
        String log = run(client, "unsampled_app", Map.of("FI_SAMPLE_RATE", "0"), true);
        assertTrue(log.contains("APP_SPAN_SAMPLED=false"), log);
        assertTrue(spans.isEmpty(), spans.toString()); assertTrue(collectorPayloads.isEmpty());
    }
    @Test void bodyLimitDoesNotClaimUnknownHostOrAffectApp() throws Exception { run("jdk", "basic", Map.of("FI_WIRE_MAX_BODY_BYTES", "10"), true); assertTrue(spans.isEmpty()); }
    @Test void legacyOkHttpRequestsMatchNoAgentBaseline() throws Exception {
        String baseline = run("okhttp", "legacy_okhttp", Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run("okhttp", "legacy_okhttp", Map.of(), true); assertTrue(spans.isEmpty());
        assertTrue(observed.contains("LEGACY_ORDINARY_STATUS=200")); assertTrue(observed.contains("LEGACY_AI_STATUS=200"));
        assertEquals(baseline.lines().filter(line -> line.startsWith("LEGACY_") || line.startsWith("BODY=")).toList(),
            observed.lines().filter(line -> line.startsWith("LEGACY_") || line.startsWith("BODY=")).toList());
    }
    @Test void defaultOkHttpConstructorIsCaptured() throws Exception { run("okhttp", "defaultclient", Map.of(), true); assertSpan(); }
    // OkHttp 4.x and 5.x on the application's classpath: folder under target, then the OkHttp, Okio and Kotlin jars the build copies there.
    static final String OKHTTP_4 = "okhttp-4,okhttp-4.12.0.jar,okio-jvm-3.6.0.jar,kotlin-stdlib-1.8.21.jar", OKHTTP_5 = "okhttp-5,okhttp-jvm-5.0.0.jar,okio-jvm-3.15.0.jar,kotlin-stdlib-2.2.0.jar";
    /** Runs the application's own OkHttp client on those jars and checks the child JVM really loaded them. */
    String runOnOkHttp(String scenario, String folder, String okhttp, String okio, String kotlin) throws Exception {
        okHttpRuntime = List.of(Path.of("target", folder, okhttp).toAbsolutePath(), Path.of("target", folder, okio).toAbsolutePath(), Path.of("target", folder, kotlin).toAbsolutePath());
        String log = run("okhttp", scenario, Map.of(), true);
        assertEquals(okhttp, line(log, "OKHTTP_JAR="), log); assertEquals(okio, line(log, "OKIO_JAR="), log); assertEquals(kotlin, line(log, "KOTLIN_JAR="), log);
        return log;
    }
    /** The same wire span the OkHttp 3 tests expect, and no line from the agent: it neither failed open nor skipped an integration. */
    void assertTracedWithoutAgentWarning(String log) {
        assertEquals(1, spans.size(), log); assertWireSpan(spans.get(0));
        assertEquals(List.of(), log.lines().filter(l -> l.contains("[traceai-auto]")).toList(), log);
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({OKHTTP_4, OKHTTP_5})
    void okHttp4And5ChatCallIsTracedAndTheResponseIsUnchanged(String folder, String okhttp, String okio, String kotlin) throws Exception {
        // run() compares the request the server received and the response body the application read.
        assertTracedWithoutAgentWarning(runOnOkHttp("okhttp_version", folder, okhttp, okio, kotlin));
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({OKHTTP_4, OKHTTP_5})
    void okHttp4And5StreamingIsByteForByteUnchanged(String folder, String okhttp, String okio, String kotlin) throws Exception {
        payload = STREAM.getBytes(StandardCharsets.UTF_8); type = "text/event-stream";
        assertTracedWithoutAgentWarning(runOnOkHttp("okhttp_version", folder, okhttp, okio, kotlin));
        assertEquals(true, attributes(spans.get(0)).get("gen_ai.request.stream")); assertTrue(attributes(spans.get(0)).containsKey("gen_ai.response.time_to_first_chunk"));
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({OKHTTP_4, OKHTTP_5})
    void okHttp4And5AsynchronousStreamingIsTraced(String folder, String okhttp, String okio, String kotlin) throws Exception {
        payload = STREAM.getBytes(StandardCharsets.UTF_8); type = "text/event-stream";
        assertTracedWithoutAgentWarning(runOnOkHttp("okhttp_version_async", folder, okhttp, okio, kotlin));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void encodedGzipStreamIsUnchangedAndParsed(String client) throws Exception {
        var out = new java.io.ByteArrayOutputStream(); try (var gzip = new java.util.zip.GZIPOutputStream(out)) { gzip.write(STREAM.getBytes(StandardCharsets.UTF_8)); }
        payload = out.toByteArray(); type = "text/event-stream"; encoding = "gzip";
        run(client, "gzip", Map.of(), true); assertSpan(); assertEquals(true, attributes(spans.get(0)).get("gen_ai.request.stream"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void ttsTranscriptObeysHideInputText(String client) throws Exception {
        payload = new byte[1024]; type = "audio/wav"; run(client, "audio", Map.of("FI_HIDE_INPUT_TEXT", "true"), true);
        assertEquals(1, spans.size()); Map<String, Object> attrs = attributes(spans.get(0)); assertFalse(attrs.containsKey("input.value")); assertFalse(attrs.containsKey("gen_ai.audio.transcript"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void sttTranscriptObeysHideOutputText(String client) throws Exception {
        payload = "{\"text\":\"private transcript\"}".getBytes(StandardCharsets.UTF_8); run(client, "multipart", Map.of("FI_HIDE_OUTPUT_TEXT", "true"), true);
        assertEquals(1, spans.size()); Map<String, Object> attrs = attributes(spans.get(0)); assertFalse(attrs.containsKey("output.value")); assertFalse(attrs.containsKey("gen_ai.audio.transcript"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void repeatedInitCannotDisableAlreadyCreatedTransport(String client) throws Exception {
        String log = run(client, "reconfigure_transport", Map.of(), true); assertEquals(2, spans.size()); spans.forEach(this::assertWireSpan); assertTrue(log.contains("APP_PROVIDER_ALIVE=true"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void repeatedInitCannotDisableWire(String client) throws Exception {
        run(client, "reconfigure_wire", Map.of(), true); assertEquals(2, spans.size()); spans.forEach(this::assertWireSpan);
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void repeatedInitializationDoesNotDuplicateOrOwnApplicationProvider(String client) throws Exception {
        String log = run(client, "reconfigure_repeat", Map.of(), true); assertEquals(2, spans.size()); assertTrue(log.contains("APP_PROVIDER_ALIVE=true"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void expectContinueEarly401IsCapturedWithoutUploading(String client) throws Exception {
        payload = "{\"error\":{\"message\":\"rejected before upload\"}}".getBytes(StandardCharsets.UTF_8);
        try (var socket = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress())) {
            socket.setSoTimeout(15000); ExecutorService worker = Executors.newSingleThreadExecutor();
            try {
                Future<String> headers = worker.submit(() -> {
                    try (var connection = socket.accept()) {
                        connection.setSoTimeout(5000); var input = connection.getInputStream(); var head = new java.io.ByteArrayOutputStream(); int tail = 0;
                        while (head.size() < 32768 && tail != 0x0d0a0d0a) { int b = input.read(); if (b < 0) throw new java.io.EOFException(); head.write(b); tail = tail << 8 | b; }
                        var output = connection.getOutputStream(); output.write(("HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: " + payload.length + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII)); output.write(payload); output.flush();
                        return head.toString(StandardCharsets.US_ASCII);
                    }
                });
                run(client, "earlyreject", Map.of("TRACEAI_TEST_PROXY_PORT", String.valueOf(socket.getLocalPort())), true);
                assertTrue(headers.get(2, TimeUnit.SECONDS).toLowerCase(Locale.ROOT).contains("expect: 100-continue")); assertEquals(1, spans.size(), spans.toString());
                var attrs = attributes(spans.get(0)); assertEquals(true, attrs.get("traceai.wire.body_unavailable")); assertEquals("401", attrs.get("error.type")); assertFalse(attrs.containsKey("input.value"));
                assertEquals(2, ((Number)WireCore.get(spans.get(0).get("status"), "code")).intValue()); assertEquals("rejected before upload", WireCore.get(spans.get(0).get("status"), "message"));
            } finally { worker.shutdownNow(); }
        }
    }
    @Test void closingRequestBodyKeepsApplicationRequestAndResponseUnchanged() throws Exception {
        run("okhttp", "verify_closing", Map.of(), true); assertSpan();
    }
    @Test void applicationNetworkSourceIOExceptionIsAbortInActualOtlp() throws Exception {
        run("okhttp", "verify_abort", Map.of(), true); assertEquals(1, spans.size());
        var span = spans.get(0); assertEquals(1, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        assertEquals(true, attributes(span).get("traceai.wire.incomplete")); assertFalse(attributes(span).containsKey("error.type"));
    }
    @Test void eagerApplicationNetworkReadAbortIsOkIncompleteWithHttpStatusAndBaselineException() throws Exception {
        holdStreams = true;
        run("okhttp", "verify_eager", Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        run("okhttp", "verify_eager", Map.of(), true);
        assertEquals(1, spans.size()); var span = spans.get(0);
        assertEquals(1, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        assertEquals(true, attributes(span).get("traceai.wire.incomplete"));
        assertEquals("200", attributes(span).get("http.response.status_code")); assertFalse(attributes(span).containsKey("error.type"));
    }
    @Test void existingWrapperSpanStaysWithTheApplicationAndTheCallUnderItIsNotTracedAgain() throws Exception {
        String log = run("jdk", "verify_wrapper", Map.of(), true); assertTrue(log.contains("WRAPPER_RAW_UNCHANGED=true"));
        // Only the control call made after the wrapper span reaches Future AGI.
        assertEquals(1, spans.size(), spans.toString()); assertWireSpan(spans.get(0)); assertNull(spans.get(0).get("parentSpanId"));
        String exported = WireCore.dump(collectorPayloads);
        for (String text : List.of("existing-wrapper", "WRAPPER_PRIVATE_PASSWORD", "WRAPPER_EVENT_PASSWORD", "PRIVATE_WRAPPER_EMBEDDING_INPUT", "PRIVATE_WRAPPER_RERANK_QUERY")) assertFalse(exported.contains(text), text);
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void parentTracestateSurvivesShadedBridge(String client) throws Exception {
        run(client, "verify_parent", Map.of(), true); assertSpan();
        assertEquals("12345678901234567890123456789012", spans.get(0).get("traceId")); assertEquals("1234567890123456", spans.get(0).get("parentSpanId"));
        assertEquals("vendor=state", spans.get(0).get("traceState"));
    }
    @ParameterizedTest @ValueSource(strings = {"verify_transport", "verify_transport_wrapped", "verify_transport_independent"})
    void nativeOkhttpFailureProvenanceSurvivesAdaptersWithoutMisclassifyingIndependentAborts(String scenario) throws Exception {
        payload = (TransportSafetyTest.chunk("partial")).getBytes(StandardCharsets.UTF_8);
        try (var socket = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress())) {
            socket.setSoTimeout(15000); var worker = Executors.newSingleThreadExecutor();
            try {
                var served = worker.submit(() -> {
                    try (var connection = socket.accept()) {
                        connection.setSoTimeout(5000); var input = connection.getInputStream(); var head = new java.io.ByteArrayOutputStream(); int tail = 0;
                        while (head.size() < 32768 && tail != 0x0d0a0d0a) { int b = input.read(); if (b < 0) throw new java.io.EOFException(); head.write(b); tail = tail << 8 | b; }
                        var length = java.util.regex.Pattern.compile("(?im)^Content-Length: *([0-9]+)").matcher(head.toString(StandardCharsets.US_ASCII));
                        if (!length.find()) throw new AssertionError("Missing request length");
                        sentRequest = new String(input.readNBytes(Integer.parseInt(length.group(1))), StandardCharsets.UTF_8);
                        var output = connection.getOutputStream(); output.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: " + (payload.length + 100) + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII)); output.write(payload); output.flush();
                    }
                    return true;
                });
                String log = run("okhttp", scenario, Map.of("TRACEAI_TEST_PROXY_PORT", String.valueOf(socket.getLocalPort())), true);
                assertTrue(served.get(2, TimeUnit.SECONDS)); assertTrue(log.contains("TRANSPORT_ORIGINAL=")); assertEquals(1, spans.size());
                boolean independent = scenario.equals("verify_transport_independent");
                var span = spans.get(0); assertEquals(independent ? 1 : 2, ((Number)WireCore.get(span.get("status"), "code")).intValue());
                assertEquals(independent ? true : null, attributes(span).get("traceai.wire.incomplete"));
                assertEquals(independent, !attributes(span).containsKey("error.type")); assertEquals("partial", attributes(span).get("output.value"));
            } finally { worker.shutdownNow(); }
        }
    }
    @Test void transparentGzipChecksumFailureKeepsApplicationBytesAndErrorAndExportsError() throws Exception {
        var encoded = new java.io.ByteArrayOutputStream();
        try (var gzip = new java.util.zip.GZIPOutputStream(encoded)) { gzip.write(STREAM.getBytes(StandardCharsets.UTF_8)); }
        payload = encoded.toByteArray(); payload[payload.length - 8] ^= 1;
        type = "text/event-stream"; encoding = "gzip";
        String baseline = run("okhttp", "verify_gzip", Map.of("FI_DISABLED_INSTRUMENTATIONS", "wire"), true);
        assertTrue(spans.isEmpty());
        String instrumented = run("okhttp", "verify_gzip", Map.of(), true);
        assertEquals(baseline.lines().filter(l -> l.startsWith("READ_ERROR=")).findFirst().orElseThrow(), instrumented.lines().filter(l -> l.startsWith("READ_ERROR=")).findFirst().orElseThrow());
        assertEquals(1, spans.size()); var span = spans.get(0);
        assertEquals(2, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        assertEquals("IOException", attributes(span).get("error.type"));
        assertEquals(true, attributes(span).get("traceai.wire.incomplete"), "A10 compressed event arrival association is unavailable");
        assertFalse(attributes(span).containsKey("gen_ai.response.time_to_first_text"));
        assertEquals("Hello world", attributes(span).get("output.value"));
    }
    @ParameterizedTest @ValueSource(strings = {"verify_gzip_success", "verify_gzip_abort"})
    void transparentGzipSuccessAndApplicationSourceAbortAreNotNativeFailures(String scenario) throws Exception {
        var encoded = new java.io.ByteArrayOutputStream();
        try (var gzip = new java.util.zip.GZIPOutputStream(encoded)) { gzip.write(STREAM.getBytes(StandardCharsets.UTF_8)); }
        payload = encoded.toByteArray(); type = "text/event-stream"; encoding = "gzip";
        run("okhttp", scenario, Map.of(), true); assertEquals(1, spans.size()); var span = spans.get(0);
        assertEquals(1, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        assertEquals(true, attributes(span).get("traceai.wire.incomplete"), "A10 marks lost compressed-event arrival association even at clean EOF");
        assertFalse(attributes(span).containsKey("gen_ai.response.time_to_first_text"));
        assertFalse(attributes(span).containsKey("error.type")); assertEquals("Hello world", attributes(span).get("output.value"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void rejectedFilenameLessTextFileNeverEntersActualOtlp(String client) throws Exception {
        status = 400; payload = "{\"error\":{\"message\":\"upload rejected\"}}".getBytes(StandardCharsets.UTF_8);
        run(client, "multipart_unlabelled", Map.of(), true); assertEquals(1, spans.size());
        var span = spans.get(0); assertEquals(2, ((Number)WireCore.get(span.get("status"), "code")).intValue());
        assertEquals(String.valueOf("PRIVATE_FILE_PAYLOAD".length()), attributes(span).get("traceai.audio.input_bytes"));
        assertEquals("text/plain", attributes(span).get("gen_ai.audio.mime_type")); assertFalse(WireCore.dump(spans).contains("PRIVATE_FILE_PAYLOAD"));
    }
    @ParameterizedTest @ValueSource(strings = {"FI_HIDE_INPUTS", "FI_HIDE_INPUT_TEXT"})
    void inputHideFlagsApplyToWireSpansWhileTheWrapperSpanStaysWithTheApplication(String flag) throws Exception {
        String log = run("jdk", "verify_wrapper", Map.of(flag, "true"), true);
        assertTrue(log.contains("WRAPPER_RAW_UNCHANGED=true")); assertEquals(1, spans.size(), spans.toString());
        String exported = WireCore.dump(collectorPayloads);
        for (String secret : List.of("existing-wrapper", "PRIVATE_WRAPPER_EMBEDDING_INPUT", "PRIVATE_WRAPPER_RERANK_QUERY", "WRAPPER_PRIVATE_PASSWORD", "WRAPPER_EVENT_PASSWORD")) assertFalse(exported.contains(secret), secret);
        var attrs = attributes(spans.get(0));
        assertEquals("wire", attrs.get("traceai.capture.layer")); assertFalse(attrs.containsKey("input.value")); assertEquals("Hello world", attrs.get("output.value"));
    }
    @Test void cohereWrapperSpanStaysWithTheApplicationUnderHideFlags() throws Exception {
        String log = run("jdk", "verify_cohere", Map.of("FI_HIDE_INPUTS", "true", "FI_HIDE_OUTPUTS", "true"), true);
        assertTrue(log.contains("COHERE_APPLICATION_UNCHANGED=true")); assertEquals(1, spans.size(), spans.toString());
        assertEquals("chat test-model", spans.get(0).get("name")); assertNull(spans.get(0).get("parentSpanId"));
        String exported = WireCore.dump(collectorPayloads);
        assertFalse(exported.contains("Cohere Chat")); assertFalse(exported.contains("existing-cohere-wrapper"));
        assertFalse(exported.contains("PRIVATE_COHERE")); assertFalse(exported.contains("PRIVATE_NORMAL_COHERE"));
        assertFalse(attributes(spans.get(0)).containsKey("input.value")); assertFalse(attributes(spans.get(0)).containsKey("output.value"));
    }
    @Test void questionMarkPromptKeepsJdkAsyncCompletionDeadlineAndBytesAtNoAgentBaseline() throws Exception {
        String baseline = run("jdk", "signed_query_async", Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run("jdk", "signed_query_async", Map.of(), true); assertSpan();
        assertTrue(baseline.contains("APP_FUTURE_TIMEOUT=false"), baseline); assertTrue(observed.contains("APP_FUTURE_TIMEOUT=false"), observed);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=")).toList());
    }
    @Test void jdkThreeArgumentSendAsyncIsCapturedExactlyOnce() throws Exception { run("jdk", "async3", Map.of(), true); assertSpan(); }
    @Test void jdk307RedirectDoesNotConcatenateReplayedRequest() throws Exception { redirect = true; run("jdk", "redirect", Map.of(), true); assertEquals(2, uploads.get()); assertSpan(); }
    @Test void okhttpAuthenticationRetryDoesNotConcatenateReplayedRequest() throws Exception { authenticate = true; run("okhttp", "authretry", Map.of(), true); assertEquals(2, uploads.get()); assertSpan(); }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void authenticatorRetryCredentialsAreScrubbedFromActualOtlp(String client) throws Exception {
        authenticate = true; echoRetryCredentials = true; run(client, "authretry", Map.of(), true);
        assertEquals(2, uploads.get()); assertNotNull(retryCredential); assertEquals(1, spans.size(), spans.toString());
        assertEquals("echo <redacted>", WireCore.get(spans.get(0).get("status"), "message"));
        assertFalse(WireCore.dump(spans).contains(retryCredential));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void inFlightStreamSurvivesProjectReconfiguration(String client) throws Exception {
        holdStreams = true; payload = STREAM.getBytes(StandardCharsets.UTF_8); type = "text/event-stream";
        run(client, "reconfigure_inflight", Map.of(), true); assertEquals(2, spans.size(), spans.toString()); for (var span : spans) assertWireSpan(span);
    }
    @ParameterizedTest @ValueSource(strings = {"coexist_premain", "coexist_existing"})
    void applicationOwnedSdkKeepsItsProcessorAndExporterAndNoneOfItsSpansReachFutureAgi(String scenario) throws Exception {
        String log = run("jdk", scenario, Map.of(), true);
        assertTrue(log.contains("APP_PROVIDER_ALIVE=true")); assertTrue(log.contains("APP_PROCESSOR_UNCHANGED=true"), log);
        // The application's LLM span and the call under it stay local; the control call after the span is traced.
        assertEquals(1, spans.size(), spans.toString()); assertWireSpan(spans.get(0)); assertNull(spans.get(0).get("parentSpanId"));
        String json = WireCore.dump(collectorPayloads);
        for (String text : List.of("application-library-call", "real-application-scope", "PRIVATE_INPUT", "PRIVATE_OUTPUT", "ATTRIBUTE_PRIVATE_PASSWORD", "STATUS_PRIVATE_TOKEN", "EVENT_PRIVATE_TOKEN", "EVENT_PRIVATE_KEY")) assertFalse(json.contains(text), text);
    }
    @ParameterizedTest @ValueSource(strings = {"verify_headers_scalars", "verify_headers_arrays"})
    void plainSpanOnAPrivateUnregisteredProviderNeverReachesFutureAgi(String scenario) throws Exception {
        String log = run("urlconnection", scenario, Map.of(), true);
        assertTrue(log.contains("REAL_HEADER_REQUEST_STATUS=200")); assertTrue(log.contains("APP_HEADERS_UNCHANGED=true"));
        HeaderPrivacyApp.HEADERS.forEach((name, value) -> assertEquals(value, receivedHeaders.get(name.toLowerCase(Locale.ROOT)), name));
        // Only the AI call made under the plain span is exported, as a wire span whose parent is that span.
        assertEquals(1, spans.size(), spans.toString()); var wire = spans.get(0); assertWireSpan(wire);
        assertEquals(line(log, "PLAIN_SPAN="), wire.get("parentSpanId"));
        String exported = WireCore.dump(collectorPayloads);
        assertFalse(exported.contains("private-provider-plain-span")); assertFalse(exported.contains("header-privacy-scope"));
        for (String value : HeaderPrivacyApp.HEADERS.values()) assertFalse(exported.contains(value), value);
        assertFalse(exported.contains("second-private-value"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void intermediatePlainApplicationSpansNeverReachFutureAgi(String client) throws Exception {
        String log = run(client, "dedup_native_async", Map.of(), true);
        assertTrue(spans.stream().allMatch(span -> "wire".equals(attributes(span).get("traceai.capture.layer"))), spans.toString());
        String exported = WireCore.dump(collectorPayloads);
        assertFalse(exported.contains("application-parent")); assertFalse(exported.contains("native-client"));
        // Deduplication reads only the active span: a call under a plain span is traced even when a library span
        // is further up. The control call after the library span is traced as well.
        assertEquals(2, spans.size(), spans.toString());
        assertEquals(1, spans.stream().filter(span -> line(log, "NATIVE=").equals(span.get("parentSpanId"))).count());
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void applicationLibrarySpanThatEchoesCredentialsNeverReachesFutureAgi(String client) throws Exception {
        assertLibrarySpanStaysLocalAndOnlyTheControlCallIsTraced(run(client, "dedup_privacy", Map.of(), true));
        String exported = WireCore.dump(collectorPayloads);
        assertFalse(exported.contains("PROVIDER_CREDENTIAL_987654321")); assertFalse(exported.contains("JSON_PASSWORD_PRIVATE_123"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"}) void wireCaptureOffExportsNothingAtAll(String client) throws Exception {
        run(client, "dedup_privacy", Map.of("FI_WIRE_CAPTURE", "false"), true);
        assertTrue(spans.isEmpty(), spans.toString()); assertTrue(collectorPayloads.isEmpty()); assertEquals(2, uploads.get());
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void wrapperInitBesideTheAgentExportsEachWrapperSpanOnceAndTheCallUnderItIsNotTracedAgain(String client) throws Exception {
        String log = run(client, "wrapper_init", Map.of(), true);
        // TraceAI.init's own exporter sends the wrapper span once; the agent adds no copy and no wire span under it.
        assertEquals(List.of("wrapper-llm-call"), wrapperSpans.stream().map(span -> span.get("name")).toList());
        assertEquals(line(log, "WRAPPER_SPAN="), wrapperSpans.get(0).get("spanId"));
        assertTrue(spans.isEmpty(), spans.toString()); assertEquals(1, uploads.get());
    }
    @ParameterizedTest @ValueSource(strings = {"parity_string", "parity_bytes", "parity_mapping", "parity_file", "parity_lines", "parity_publisher", "parity_stream", "parity_unread"})
    void jdkBodyHandlersAndSubscribersMatchNoAgentBaseline(String scenario) throws Exception { parity("jdk", scenario); }
    @ParameterizedTest @ValueSource(strings = {"parity_string", "parity_bytes", "parity_mapping", "parity_stream", "parity_unread"})
    void okhttpResponseBodySubclassIdentityAndLazyAccessMatchNoAgentBaseline(String scenario) throws Exception { parity("okhttp", scenario); }
    void parity(String client, String scenario) throws Exception {
        String baseline = run(client, scenario, Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run(client, scenario, Map.of(), true);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("PARITY=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("PARITY=")).toList());
        assertEquals(1, spans.size(), observed);
        if (scenario.equals("parity_unread")) {
            // JDK may already have delivered the entire body to its own queue without an application read.
            var attrs = attributes(spans.get(0));
            assertTrue(Boolean.TRUE.equals(attrs.get("traceai.wire.incomplete")) || "Hello world".equals(attrs.get("output.value")));
            assertEquals(1, ((Number)WireCore.get(spans.get(0).get("status"), "code")).intValue());
        } else assertWireSpan(spans.get(0));
        if (scenario.equals("parity_mapping")) {
            long parseMillis = Long.parseLong(observed.lines().filter(line -> line.startsWith("PARSE_START=")).findFirst().orElseThrow().substring(12));
            assertTrue(Long.parseLong((String)spans.get(0).get("endTimeUnixNano")) <= (parseMillis + 2) * 1_000_000L, "Application parsing must not extend span duration");
        }
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void processRegistryProtectsUnrelatedThreadsAfterSuppressedAsyncCalls(String client) throws Exception {
        String baseline = run(client, "registry", Map.of("TRACEAI_TEST_NO_AGENT", "true", RegistryApp.ENV_NAME, RegistryApp.ENV), true); assertTrue(spans.isEmpty());
        String observed = run(client, "registry", Map.of(RegistryApp.ENV_NAME, RegistryApp.ENV), true);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=")).toList());
        assertOnlyWireSpans(1, observed);
        String exported = WireCore.dump(collectorPayloads);
        for (String secret : List.of(RegistryApp.HEADER, RegistryApp.QUERY, RegistryApp.API, RegistryApp.ENV)) assertFalse(exported.contains(secret), secret);
    }
    /** Application spans (registry-library, registry-native, registry-unrelated) never reach Future AGI. */
    void assertOnlyWireSpans(int count, String log) {
        assertEquals(count, spans.size(), log + spans);
        assertTrue(spans.stream().allMatch(span -> "wire".equals(attributes(span).get("traceai.capture.layer")) || "<redacted>".equals(span.get("name"))), spans.toString());
        String exported = WireCore.dump(collectorPayloads);
        for (String text : List.of("registry-", "PUBLIC_BODY_CONTROL")) assertFalse(exported.contains(text), text);
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void proxyCredentialsAreObservedUnderSuppressionAndProtectLaterSpansWithBaselineParity(String client) throws Exception {
        var options = Map.of(RegistryApp.ENV_NAME, RegistryApp.ENV, "TRACEAI_TEST_PROXY_PORT", Integer.toString(server.getAddress().getPort()));
        var baselineOptions = new HashMap<>(options); baselineOptions.put("TRACEAI_TEST_NO_AGENT", "true");
        String baseline = run(client, "registry_proxy", baselineOptions, true); assertTrue(spans.isEmpty());
        assertEquals(RegistryApp.PROXY, receivedHeaders.get("proxy-authorization"));
        String observed = run(client, "registry_proxy", options, true);
        assertEquals(RegistryApp.PROXY, receivedHeaders.get("proxy-authorization")); assertEquals(2, uploads.get());
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=")).toList());
        assertOnlyWireSpans(1, observed);
        String exported = WireCore.dump(collectorPayloads);
        for (String secret : List.of(RegistryApp.PROXY, RegistryApp.PROXY_PAYLOAD, RegistryApp.QUERY, RegistryApp.API, RegistryApp.ENV)) assertFalse(exported.contains(secret), secret);
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({"jdk,true", "okhttp,true", "jdk,false", "okhttp,false"})
    void awsSessionTokensAreObservedUnderSuppressionWithBaselineParity(String client, boolean wireEnabled) throws Exception {
        var options = Map.of("FI_WIRE_CAPTURE", Boolean.toString(wireEnabled));
        var baselineOptions = new HashMap<>(options); baselineOptions.put("TRACEAI_TEST_NO_AGENT", "true");
        String baseline = run(client, "registry_session", baselineOptions, true); assertTrue(spans.isEmpty());
        assertEquals(RegistryApp.SESSION, receivedHeaders.get("x-amz-security-token"));
        String observed = run(client, "registry_session", options, true);
        assertEquals(RegistryApp.SESSION, receivedHeaders.get("x-amz-security-token")); assertEquals(2, uploads.get());
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList());
        assertOnlyWireSpans(wireEnabled ? 1 : 0, observed);
        assertFalse(WireCore.dump(collectorPayloads).contains(RegistryApp.SESSION));
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({"jdk,true", "okhttp,true", "jdk,false", "okhttp,false"})
    void s6AuthTokenHeadersProtectLaterSpansUnderSuppressionEvenWhenWireDisabled(String client, boolean wireEnabled) throws Exception {
        var options = Map.of("FI_WIRE_CAPTURE", Boolean.toString(wireEnabled));
        var baselineOptions = new HashMap<>(options); baselineOptions.put("TRACEAI_TEST_NO_AGENT", "true");
        String baseline = run(client, "registry_auth", baselineOptions, true); assertTrue(spans.isEmpty());
        String observed = run(client, "registry_auth", options, true);
        assertEquals(RegistryApp.SESSION, receivedHeaders.get("x-auth-token")); assertEquals(2, uploads.get());
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList());
        assertOnlyWireSpans(wireEnabled ? 1 : 0, observed);
        assertFalse(WireCore.dump(collectorPayloads).contains(RegistryApp.SESSION));
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({"jdk,registry_bodypasswd", "okhttp,registry_bodypasswd", "jdk,registry_bodyarray", "okhttp,registry_bodyarray"})
    void s6ParsedPasswdAndCredentialArraysProtectUnrelatedSpansWithBaselineParity(String client, String scenario) throws Exception {
        String baseline = run(client, scenario, Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run(client, scenario, Map.of(), true);
        assertEquals(2, uploads.get());
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList());
        assertOnlyWireSpans(1, observed); assertEquals("wire", attributes(spans.get(0)).get("traceai.capture.layer"));
        assertFalse(WireCore.dump(collectorPayloads).contains(RegistryApp.BODY_SECRET));
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({"jdk,0", "okhttp,0", "jdk,8200", "okhttp,8200"})
    void parsedBodyBudgetExhaustionFailsClosedForLaterSpansWithoutChangingRequests(String client, int padding) throws Exception {
        String scenario = "registry_body" + padding;
        String baseline = run(client, scenario, Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run(client, scenario, Map.of(), true);
        assertEquals(2, uploads.get()); assertTrue(sentRequest.length() < 20000);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("APP_CREDENTIALS_UNCHANGED=")).toList());
        assertOnlyWireSpans(1, observed);
        assertFalse(WireCore.dump(collectorPayloads).contains(RegistryApp.BODY_SECRET));
        if (padding == 0) assertEquals("wire", attributes(spans.get(0)).get("traceai.capture.layer"));
        else for (var span : spans) {
            assertEquals("<redacted>", span.get("name"));
            for (String content : List.of("attributes", "status", "events", "links")) assertFalse(span.containsKey(content), content);
            assertEquals(32, span.get("traceId").toString().length()); assertEquals(16, span.get("spanId").toString().length());
            assertTrue(Long.parseLong(span.get("startTimeUnixNano").toString()) <= Long.parseLong(span.get("endTimeUnixNano").toString()));
        }
    }
    @ParameterizedTest @org.junit.jupiter.params.provider.CsvSource({"jdk,true", "okhttp,true", "jdk,false", "okhttp,false"})
    void applicationMessageSpansNeverReachFutureAgiWithOrWithoutImageHiding(String client, boolean hide) throws Exception {
        String baseline = run(client, "registry_media", Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run(client, "registry_media", Map.of("FI_HIDE_INPUT_IMAGES", Boolean.toString(hide), RegistryApp.ENV_NAME, RegistryApp.ENV), true);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("MEDIA_UNCHANGED=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("MEDIA_UNCHANGED=")).toList());
        assertOnlyWireSpans(1, observed); assertEquals(2, uploads.get());
        String exported = WireCore.dump(collectorPayloads);
        for (String text : List.of(RegistryApp.IMAGE, "QUJD", "PUBLIC_MESSAGE_TEXT", "PUBLIC_ARRAY_TEXT", "PUBLIC_FLAT_TEXT")) assertFalse(exported.contains(text), text);
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void paddedMagicMediaIsPrivateInShadedAgentWithoutChangingApplicationData(String client) throws Exception {
        payload = BASIC.replace("Hello world", MagicMediaApp.MEDIA).getBytes(StandardCharsets.UTF_8);
        String baseline = run(client, "magic_media", Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run(client, "magic_media", Map.of(), true);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("MEDIA_UNCHANGED=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=") || line.startsWith("REQUEST_WRITES=") || line.startsWith("MEDIA_UNCHANGED=")).toList());
        assertTrue(observed.contains("MEDIA_UNCHANGED=true")); assertTrue(observed.contains("REQUEST_WRITES=1"));
        // Only the wire span is exported; the application span that carries the media stays local.
        assertEquals(2, uploads.get()); assertEquals(1, spans.size(), observed);
        String exported = WireCore.dump(collectorPayloads), omitted = "<omitted: 44 base64 chars>";
        assertFalse(exported.contains(MagicMediaApp.MEDIA));
        assertFalse(exported.contains(java.net.URLEncoder.encode(MagicMediaApp.MEDIA, StandardCharsets.UTF_8)));
        assertFalse(exported.contains("magic-media-application")); assertFalse(exported.contains("PUBLIC_MAGIC_CONTROL"));
        var wire = spans.get(0); assertEquals("wire", attributes(wire).get("traceai.capture.layer"));
        assertTrue(attributes(wire).get("input.value").toString().contains(omitted));
        assertEquals(omitted, attributes(wire).get("output.value"));
    }
    @ParameterizedTest @ValueSource(strings = {"jdk", "okhttp"})
    void hiddenShortOllamaImagesNeverReachInvocationParametersInShadedAgent(String client) throws Exception {
        payload = "{\"response\":\"Hello world\",\"model\":\"test-model\",\"done\":true}".getBytes(StandardCharsets.UTF_8);
        var options = Map.of("FI_HIDE_INPUT_IMAGES", "true", "TRACEAI_TEST_PROXY_PORT", Integer.toString(server.getAddress().getPort()));
        String baseline = run(client, "ollama_image", Map.of("TRACEAI_TEST_NO_AGENT", "true", "TRACEAI_TEST_PROXY_PORT", Integer.toString(server.getAddress().getPort())), true); assertTrue(spans.isEmpty());
        String observed = run(client, "ollama_image", options, true);
        assertEquals(baseline.lines().filter(line -> line.startsWith("BODY=")).toList(), observed.lines().filter(line -> line.startsWith("BODY=")).toList());
        assertEquals(1, spans.size()); var attrs = attributes(spans.get(0));
        assertFalse(attrs.containsKey("input.value")); assertEquals("ollama", attrs.get("gen_ai.provider.name"));
        assertFalse(WireCore.dump(List.of(spans, scopes, resources)).contains(PlainApp.image()));
        assertFalse(attrs.get("gen_ai.request.parameters").toString().contains("images")); assertTrue(attrs.get("gen_ai.request.parameters").toString().contains("temperature"));
    }
    @Test void repeatedDataUrlPrefixesPreserveAsyncCompletionDeadlineAgainstBaseline() throws Exception {
        String baseline = run("jdk", "data_url_async", Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run("jdk", "data_url_async", Map.of(), true);
        assertTrue(baseline.contains("APP_FUTURE_TIMEOUT=false"), baseline); assertTrue(observed.contains("APP_FUTURE_TIMEOUT=false"), observed);
        assertSpan(); assertTrue(attributes(spans.get(0)).get("input.value").toString().contains("data:a".repeat(10)));
    }
    @ParameterizedTest @ValueSource(strings = {"metadata_ai", "metadata_ordinary"})
    void statefulOkHttpBodyMetadataIsReadOnlyOnceLikeBaseline(String scenario) throws Exception {
        String baseline = run("okhttp", scenario, Map.of("TRACEAI_TEST_NO_AGENT", "true"), true); assertTrue(spans.isEmpty());
        String observed = run("okhttp", scenario, Map.of(), true);
        assertTrue(baseline.contains("GETTER_CALLS=1"), baseline); assertTrue(observed.contains("GETTER_CALLS=1"), observed);
        if (scenario.equals("metadata_ai")) assertSpan(); else assertTrue(spans.isEmpty());
    }
    @Test void httpError() throws Exception { status = 401; payload = "{\"error\":{\"message\":\"Invalid API key\"}}".getBytes(StandardCharsets.UTF_8); run("jdk", "basic", Map.of(), true); assertEquals(1, spans.size()); assertEquals("401", attributes(spans.get(0)).get("error.type")); assertEquals("Invalid API key", WireCore.get(spans.get(0).get("status"), "message")); }
}
