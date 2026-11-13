package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundFiveRegressionTest {
    @Test void registryEvictsLeastRecentlyObservedValueAndReusesImmutableSnapshots() {
        var registry = new KnownSecrets();
        registry.register("short"); registry.register(null);
        assertEquals("short", registry.snapshot().scrub("short"));
        for (int i = 0; i < 1024; i++) registry.register("LRU_VALUE_" + i + "_END");
        var snapshot = registry.snapshot(); assertSame(snapshot, registry.snapshot());
        registry.register("LRU_VALUE_0_END"); registry.register("LRU_VALUE_1024_END");
        String scrubbed = registry.snapshot().scrub("LRU_VALUE_0_END LRU_VALUE_1_END LRU_VALUE_1024_END");
        assertEquals("<redacted> LRU_VALUE_1_END <redacted>", scrubbed);
        assertEquals(1024, registry.snapshot().secrets().size());
        assertEquals("<redacted>", snapshot.scrub("LRU_VALUE_1_END"), "old immutable snapshots remain consistent");
    }
    @Test void registryReplacesLongestFirstAndHandlesRepeatedPrefixCredentialsLinearly() {
        var registry = new KnownSecrets(); registry.register("LONG_SECRET"); registry.register("LONG_SECRET_SUFFIX");
        assertEquals("<redacted> <redacted>", registry.snapshot().scrub("LONG_SECRET_SUFFIX LONG_SECRET"));
        registry.register("a".repeat(65536) + "b");
        assertTimeout(Duration.ofSeconds(1), () -> assertEquals("a".repeat(200000), registry.snapshot().scrub("a".repeat(200000))));
    }
    @Test void registrySeedsEveryCredentialEnvironmentMarkerAndConfiguredFiKeys() {
        var registry = new KnownSecrets(); Map<String, String> environment = new HashMap<>();
        for (String marker : List.of("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")) environment.put("app_" + marker, "ENV_VALUE_" + marker);
        environment.put("UNMARKED", "PUBLIC_UNMARKED"); environment.put("SHORT_KEY", "short");
        registry.seed(AutoConfig.resolve(Map.of("api_key", "CONFIG_FI_API", "secret_key", "CONFIG_FI_SECRET"), Map.of()), environment);
        for (var entry : environment.entrySet()) assertEquals(entry.getKey().equals("UNMARKED") || entry.getKey().equals("SHORT_KEY") ? entry.getValue() : "<redacted>", registry.snapshot().scrub(entry.getValue()));
        assertEquals("<redacted> <redacted>", registry.snapshot().scrub("CONFIG_FI_API CONFIG_FI_SECRET"));
    }
    @Test void concurrentRegistrationAndExportSnapshotsAreSafe() throws Exception {
        var registry = new KnownSecrets(); var pool = Executors.newFixedThreadPool(4);
        try {
            List<Future<?>> jobs = new ArrayList<>();
            for (int worker = 0; worker < 4; worker++) { final int id = worker; jobs.add(pool.submit(() -> {
                for (int i = 0; i < 100; i++) { String secret = "THREADED_VALUE_" + id + "_" + i + "_END"; registry.register(secret); assertEquals("<redacted>", registry.snapshot().scrub(secret)); }
            })); }
            for (var job : jobs) job.get(5, TimeUnit.SECONDS);
            assertEquals(400, registry.snapshot().secrets().size());
        } finally { pool.shutdownNow(); }
    }
    @Test void everyExportSurfaceUsesSecretsRegisteredAfterSpanCreationWithoutMutation() throws Exception {
        assertAllSurfacesScrubbed("ROUND5_LATE_REGISTERED_SECRET", () -> TraceAIAuto.registerSecret("ROUND5_LATE_REGISTERED_SECRET"));
    }
    @Test void proxyAuthorizationRegistersFullHeaderAndSchemePayloadForEveryExportSurface() throws Exception {
        String payload = Base64.getEncoder().encodeToString("probe:proxy-secret-802701xx".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        assertAllSurfacesScrubbed("Basic " + payload, () -> KnownSecrets.observe(Map.of("pRoXy-AuThOrIzAtIoN", "Basic " + payload), null));
        assertAllSurfacesScrubbed(payload, () -> {});
        var privacy = ExportPrivacy.privacy(AutoConfig.resolve(Map.of(), Map.of()));
        for (String name : List.of("Proxy-Authorization", "PROXY_AUTHORIZATION", "http.request.header.proxy-authorization")) {
            assertEquals("<redacted>", privacy.clean(name, "UNOBSERVED_PROXY_PASSWORD"));
            assertFalse(privacy.text(WireCore.dump(Map.of(name, "UNOBSERVED_PROXY_PASSWORD"))).contains("UNOBSERVED_PROXY_PASSWORD"));
        }
        KnownSecrets.observe(Map.of("Proxy-Authorization", "Basic abc"), null);
        assertEquals("<redacted>", ExportPrivacy.privacy(AutoConfig.resolve(Map.of(), Map.of())).text("Basic abc"), "Register the full value even when the scheme payload is shorter than eight characters");
    }
    @Test void awsSessionTokenHeadersRegisterAndScrubAllExportSurfacesAndCredentialFields() throws Exception {
        String secret = "ROUND5_AWS_SESSION_CREDENTIAL";
        assertAllSurfacesScrubbed(secret, () -> KnownSecrets.observe(Map.of("x-AmZ-SeCuRiTy-ToKeN", secret), null));
        var privacy = ExportPrivacy.privacy(AutoConfig.resolve(Map.of(), Map.of()));
        for (String name : List.of("X-Amz-Security-Token", "X_AMZ_SECURITY_TOKEN", "http.request.header.x-amz-security-token")) {
            assertEquals("<redacted>", privacy.clean(name, "UNOBSERVED_AWS_SESSION_CREDENTIAL"));
            assertEquals("<redacted>", privacy.clean(name, List.of("UNOBSERVED_AWS_SESSION_CREDENTIAL")));
            assertFalse(privacy.text(WireCore.dump(Map.of(name, "UNOBSERVED_AWS_SESSION_CREDENTIAL"))).contains("UNOBSERVED_AWS_SESSION_CREDENTIAL"));
        }
    }
    @Test void parsedBodyObservationNeverSilentlyTruncatesAtNodeBudget() {
        for (int padding : List.of(0, 8187, 8188, 8200)) {
            var registry = new KnownSecrets(); Map<String, Object> body = new LinkedHashMap<>();
            body.put("model", "test-model"); body.put("messages", List.of());
            body.put("padding", Collections.nCopies(padding, 0)); body.put("password", "ROUND5_TRAILING_BODY_CREDENTIAL");
            registry.observeBody(WireCore.parse(WireCore.dump(body)));
            assertEquals(padding > 8187, registry.snapshot().unavailable(), "padding=" + padding);
            var privacy = new WirePrivacy(registry.snapshot(), false);
            assertEquals("<redacted>", privacy.text("ROUND5_TRAILING_BODY_CREDENTIAL"));
            assertEquals(padding > 8187, privacy.failed, "exhaustion must fail closed, not merely scrub fields already seen");
        }
    }
    @Test void parsedBodyDepthAndCycleLimitsFailClosedWithoutEscapingToApplication() {
        for (int depth : List.of(32, 33)) {
            var registry = new KnownSecrets(); Object nested = "public";
            for (int i = 0; i < depth; i++) nested = List.of(nested);
            registry.observeBody(nested);
            assertEquals(depth > 32, registry.snapshot().unavailable());
        }
        for (boolean list : List.of(false, true)) {
            var registry = new KnownSecrets();
            if (list) { List<Object> cycle = new ArrayList<>(); cycle.add(cycle); registry.observeBody(cycle); }
            else { Map<String, Object> cycle = new LinkedHashMap<>(); cycle.put("cycle", cycle); registry.observeBody(cycle); }
            assertTrue(registry.snapshot().unavailable());
            assertThrows(IllegalStateException.class, () -> registry.snapshot().scrub("public"));
        }
    }
    private void assertAllSurfacesScrubbed(String secret, Runnable register) throws Exception {
        var attrs = Attributes.builder().put("value", secret).put("name-" + secret, secret).put(AttributeKey.stringArrayKey("array"), List.of(secret, "public")).build();
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setResource(Resource.create(attrs, "https://schema/" + secret)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var context = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.builder().put("vendor", secret).build());
            var tracer = provider.tracerBuilder("scope-" + secret).setInstrumentationVersion(secret).setSchemaUrl("https://scope/" + secret).build();
            var span = tracer.spanBuilder("span-" + secret).setParent(io.opentelemetry.context.Context.root().with(Span.wrap(context))).addLink(context, attrs).setAllAttributes(attrs).startSpan();
            span.setStatus(StatusCode.ERROR, secret); span.addEvent("event-" + secret, attrs); span.end();
            var original = memory.getFinishedSpanItems().get(0);
            register.run();
            var scope = io.opentelemetry.sdk.common.InstrumentationScopeInfo.builder("scope-" + secret).setVersion(secret).setAttributes(attrs).build();
            var data = (io.opentelemetry.sdk.trace.data.SpanData)java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{io.opentelemetry.sdk.trace.data.SpanData.class}, (proxy, method, args) -> method.getName().equals("getInstrumentationScopeInfo") ? scope : method.invoke(original, args));
            String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of(), Map.of())).serialize(data));
            assertFalse(json.contains(secret)); assertTrue(json.contains("<redacted>")); assertTrue(json.contains("public"));
            assertEquals(secret, original.getAttributes().get(AttributeKey.stringKey("value"))); assertEquals(secret, original.getStatus().getDescription());
            assertEquals(secret, original.getLinks().get(0).getSpanContext().getTraceState().get("vendor"));
        }
    }
    @Test void outgoingQueryHeadersAndParsedBodyCredentialsScrubUnrelatedLaterContent() {
        KnownSecrets.observe(Map.of("Authorization", "Bearer ROUND5_HEADER_SECRET", "X-Api-Key", "ROUND5_SECOND_HEADER"), "http://127.0.0.1/?API%5FKEY=ROUND5_QUERY_SECRET&sig=ROUND5_SIGNED_SECRET");
        var req = new WireCore.Request("POST", "http://127.0.0.1/v1/chat/completions");
        req.headers = Map.of("content-type", "application/json");
        req.body = WireCore.dump(Map.of("messages", List.of(), "nested", Map.of("authorization_token", "ROUND5_BODY_SECRET"))).getBytes(java.nio.charset.StandardCharsets.UTF_8);
        var resp = new WireCore.Response(); resp.status = 200; resp.headers = Map.of("content-type", "application/json"); resp.body = "{}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        assertNotNull(WireCore.build(WireCore.match(req.method, req.url), req, resp));
        var privacy = ExportPrivacy.privacy(AutoConfig.resolve(Map.of(), Map.of()));
        for (String secret : List.of("ROUND5_HEADER_SECRET", "ROUND5_SECOND_HEADER", "ROUND5_QUERY_SECRET", "ROUND5_SIGNED_SECRET", "ROUND5_BODY_SECRET")) assertEquals("echo <redacted>", privacy.text("echo " + secret));
    }
    @Test void invocationParametersNeverContainProviderContentEvenInsideAllowedContainers() throws Exception {
        var image = new java.awt.image.BufferedImage(1, 1, java.awt.image.BufferedImage.TYPE_INT_RGB); var bytes = new java.io.ByteArrayOutputStream();
        javax.imageio.ImageIO.write(image, "png", bytes); String png = Base64.getEncoder().encodeToString(bytes.toByteArray());
        Map<String, Object> body = new LinkedHashMap<>(); body.put("model", "llama"); body.put("prompt", "PRIVATE_PROMPT"); body.put("images", List.of(png));
        body.put("temperature", .7); body.put("options", Map.of("temperature", .5, "images", List.of(png), "messages", "PRIVATE_MESSAGE", "input", "PRIVATE_INPUT"));
        for (String provider : List.of("ollama", "openai", "gemini", "anthropic", "bedrock")) {
            String clean = new WirePrivacy(Map.of(), null).text(WireCore.dump(body));
            assertFalse(clean.contains(png), provider);
            String parameters = (String)new WirePrivacy(Map.of(), null).clean("gen_ai.request.parameters", WireCore.dump(body));
            for (String content : List.of(png, "PRIVATE_PROMPT", "PRIVATE_MESSAGE", "PRIVATE_INPUT", "images", "messages", "input", "prompt")) assertFalse(parameters.contains(content), provider + ":" + content);
            assertTrue(parameters.contains("temperature")); assertTrue(parameters.contains("0.5"));
        }
        var request = new WireCore.Request("POST", "http://ollama.com/api/generate"); request.headers = Map.of("content-type", "application/json"); request.body = WireCore.dump(body).getBytes(java.nio.charset.StandardCharsets.UTF_8);
        var response = new WireCore.Response(); response.status = 200; response.headers = Map.of("content-type", "application/json"); response.body = "{\"response\":\"public\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        var result = WireCore.build(WireCore.match(request.method, request.url), request, response); assertNotNull(result);
        assertFalse(String.valueOf(result.attributes().get("gen_ai.request.parameters")).contains("images"));
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("ollama").spanBuilder("generate").startSpan();
            span.setAttribute("input.value", WireCore.dump(body)); span.setAttribute("gen_ai.request.parameters", WireCore.dump(body)); span.end();
            String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of("hide_input_images", true), Map.of())).serialize(memory.getFinishedSpanItems().get(0)));
            assertFalse(json.contains(png)); assertFalse(json.contains("PRIVATE_PROMPT")); assertTrue(json.contains("temperature"));
        }
    }
    @Test void everyScrubbingScannerIsBoundedOnRepeatedAndUnterminatedPrefixes() {
        var privacy = new WirePrivacy(Map.of(), null);
        assertTimeout(Duration.ofSeconds(2), () -> {
            for (int size : List.of(8192, 16384, 32768, 65536)) for (String text : List.of("data:a".repeat(size / 6), "\"a".repeat(size / 2), "\\".repeat(size), "?".repeat(size))) assertEquals(text, privacy.text(text));
            assertEquals("before <omitted: 4 base64 chars> after", privacy.text("before data:image/png;base64,QUJD after"));
            assertEquals("before <omitted: data URI> after", privacy.text("before data:audio/wav,%00%01%02 after"));
            assertFalse(privacy.text("error {\"audio_base64\":\"QUJDQUJD").contains("QUJD"));
            assertEquals("<omitted: 4 base64 chars>", privacy.clean("input.audio.0", "QUJD"));
            // S6: short unrecognized base64 needs an audio field or MIME hint; a wrapper alone is not media.
            assertEquals("QUJD", privacy.clean("request.inlineData.data", "QUJD"));
            var scope = privacy.scope(Map.of("request.inlineData.mimeType", "image/png"), "", WirePrivacy.Scope.EMPTY);
            assertEquals("<omitted: 4 base64 chars>", privacy.clean("request.inlineData.data", "QUJD", scope, true));
            assertFalse(privacy.text("error {\"password\":\"ROUND5_TRUNCATED_SECRET").contains("ROUND5_TRUNCATED_SECRET"));
        });
    }
    @Test void hiddenImagesInsideMessageJsonHonorFlagAcrossProvidersArraysAndExportSurfacesWithoutMutation() throws Exception {
        String uri = "https://image.example/PRIVATE_IMAGE";
        List<Object> blocks = List.of(
            Map.of("type", "image_url", "image_url", Map.of("url", uri)),
            Map.of("type", "input_image", "url", uri),
            Map.of("type", "image", "source", Map.of("type", "url", "url", uri)),
            Map.of("fileData", Map.of("mimeType", "image/png", "fileUri", uri)),
            Map.of("mimeType", "image/png", "uri", uri),
            Map.of("image", Map.of("source", Map.of("s3Location", Map.of("uri", uri)))),
            Map.of("images", List.of(uri)), Map.of("file_uri", uri),
            Map.of("gen_ai.input.messages.0.file.uri", uri));
        for (Object block : blocks) {
            String messages = WireCore.dump(List.of(Map.of("role", "user", "content", List.of(Map.of("type", "text", "text", "PUBLIC_MESSAGE_TEXT"), block, Map.of("type", "input_audio", "input_audio", Map.of("data", "QUJD", "format", "wav"))))));
            var attrs = Attributes.builder().put("gen_ai.input.messages", messages)
                .put(AttributeKey.stringArrayKey("gen_ai.input.messages.array"), List.of(messages, "PUBLIC_ARRAY_TEXT")).build();
            var memory = InMemorySpanExporter.create();
            try (var provider = SdkTracerProvider.builder().setResource(Resource.create(attrs)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
                var context = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.getDefault());
                var span = provider.get("image-messages").spanBuilder("image-messages").setAllAttributes(attrs).addLink(context, attrs).startSpan();
                span.addEvent("image-messages", attrs); span.end();
                var original = memory.getFinishedSpanItems().get(0);
                var scope = io.opentelemetry.sdk.common.InstrumentationScopeInfo.builder("image-messages").setAttributes(attrs).build();
                var data = (io.opentelemetry.sdk.trace.data.SpanData)java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{io.opentelemetry.sdk.trace.data.SpanData.class}, (proxy, method, args) -> method.getName().equals("getInstrumentationScopeInfo") ? scope : method.invoke(original, args));
                for (boolean hide : List.of(false, true)) {
                    var config = AutoConfig.resolve(Map.of("hide_input_images", hide), Map.of());
                    for (var candidate : List.of(data, ApplicationOtel.snapshot(data, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
                        String json = WireCore.dump(new OtlpJsonExporter(config).serialize(candidate));
                        assertEquals(!hide, json.contains(uri), String.valueOf(block));
                        assertFalse(json.contains("QUJD")); assertTrue(json.contains("PUBLIC_MESSAGE_TEXT")); assertTrue(json.contains("PUBLIC_ARRAY_TEXT"));
                    }
                }
                assertEquals(attrs.asMap(), original.getAttributes().asMap()); assertEquals(attrs.asMap(), original.getResource().getAttributes().asMap());
                assertEquals(attrs.asMap(), original.getEvents().get(0).getAttributes().asMap()); assertEquals(attrs.asMap(), original.getLinks().get(0).getAttributes().asMap());
            }
        }
    }
    @Test void imageHideScannerHandlesEmbeddedAndTruncatedJsonWithoutBacktracking() {
        var privacy = ExportPrivacy.privacy(AutoConfig.resolve(Map.of("hide_input_images", true), Map.of()));
        assertTimeout(Duration.ofSeconds(2), () -> {
            for (String json : List.of("{\"type\":\"image_url\",\"url\":\"https://image.example/PRIVATE_IMAGE\"}", "{\"fileUri\":\"https://image.example/PRIVATE_IMAGE\"}")) {
                assertFalse(privacy.text("error: " + json).contains("PRIVATE_IMAGE"));
                assertFalse(privacy.text("error: " + json.substring(0, json.length() - 1)).contains("PRIVATE_IMAGE"));
            }
            assertEquals("data:a".repeat(5461), privacy.text("data:a".repeat(5461)));
            assertEquals("?".repeat(32768), privacy.text("?".repeat(32768)));
            assertEquals("PUBLIC_TEXT", privacy.clean("gen_ai.input.messages.0.message.content", "PUBLIC_TEXT"));
            Map<String, Object> cycle = new HashMap<>(); cycle.put("cycle", cycle);
            assertTrue(WireCore.dump(privacy.clean(cycle)).contains("<redacted>"));
        });
    }
    @Test void registryFailuresAndOversizeValuesFailClosedWithoutDiagnostics() {
        var registry = new KnownSecrets(); registry.register("x".repeat(4 * 1024 * 1024 + 1));
        assertThrows(IllegalStateException.class, () -> registry.snapshot().scrub("public"));
        var failed = new KnownSecrets();
        failed.observeBody(new HashMap<String, Object>() { @Override public Set<Entry<String, Object>> entrySet() { throw new IllegalStateException("synthetic registry access failure"); } });
        assertThrows(IllegalStateException.class, () -> failed.snapshot().scrub("public"));
    }
}
