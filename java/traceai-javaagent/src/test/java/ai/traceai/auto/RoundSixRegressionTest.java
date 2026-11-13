package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundSixRegressionTest {
    @Test void catalogNamesAndExclusionsAgreeAcrossObservationBodyAndExport() throws Exception {
        Map<String, Object> catalog;
        try (var in = WirePrivacy.class.getResourceAsStream("privacy.json")) { catalog = WireCore.map(WireCore.parse(new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8))); }
        Map<String, Object> credentials = WireCore.map(catalog.get("credentials"));
        Set<String> names = new LinkedHashSet<>();
        for (Object name : WireCore.list(credentials.get("seed_names"))) names.add((String)name);
        for (Object pattern : WireCore.list(credentials.get("patterns"))) names.add((String)WireCore.get(pattern, "example"));
        for (Object exclusion : WireCore.list(credentials.get("exclusions"))) for (Object name : WireCore.list(WireCore.get(exclusion, "examples"))) names.add((String)name);
        for (Object exclusion : WireCore.list(credentials.get("contextual_exclusions"))) for (Object name : WireCore.list(WireCore.get(exclusion, "examples"))) names.add((String)name);
        names.addAll(List.of("usage.tokens.api_key", "secret.input_tokens", "x_auth_token", "credentials.0.input_tokens", "token[0].value"));
        String value = "S6_NAME_PARITY_PRIVATE_81973264";
        for (String name : names) {
            boolean credential = PrivacyCatalog.INSTANCE.credentialName(name);
            String expected = credential ? "<redacted>" : value;
            var header = new KnownSecrets(); header.observeRequest(Map.of(name, value), null);
            assertEquals(expected, header.snapshot().scrub(value), "header " + name);
            var body = new KnownSecrets(); body.observeBody(Map.of(name, List.of(Map.of("value", List.of(value)))));
            assertEquals(expected, body.snapshot().scrub(value), "body ancestry " + name);
            var query = new KnownSecrets(); query.observeRequest(Map.of(), "http://127.0.0.1/?" + URLEncoder.encode(name, StandardCharsets.UTF_8) + "=" + value);
            assertEquals(PrivacyCatalog.INSTANCE.queryName(name) ? "<redacted>" : value, query.snapshot().scrub(value), "query " + name);
            var privacy = new WirePrivacy(new KnownSecrets().snapshot(), false);
            assertEquals(expected, privacy.clean(name, value), "attribute " + name);
            assertEquals(Map.of(name, expected), privacy.clean(Map.of(name, value)), "JSON " + name);
        }
    }
    @Test void s5EnvironmentSeedingRemainsBroadDespiteTelemetryNameExclusions() {
        var registry = new KnownSecrets();
        registry.seed(null, Map.of("MONKEY", "ENV_KEY_SUBSTRING_81973264", "MAX_TOKENS", "ENV_TOKEN_SUBSTRING_81973264", "AUTHOR_NAME", "ENV_AUTH_SUBSTRING_81973264"));
        for (String value : List.of("ENV_KEY_SUBSTRING_81973264", "ENV_TOKEN_SUBSTRING_81973264", "ENV_AUTH_SUBSTRING_81973264")) assertEquals("<redacted>", registry.snapshot().scrub(value));
        assertFalse(PrivacyCatalog.INSTANCE.credentialName("max_tokens"));
    }
    @Test void malformedPercentEscapesDoNotDefeatNameDecodingOrObservation() {
        String secret = "S6_URL_CREDENTIAL_81973264";
        for (String name : List.of("%74oken%ZZ", "%FF%74oken", "client_%73ecret%", "password%2")) {
            String url = "https://example.invalid/?" + name + "=" + secret + "&public=keep";
            var registry = new KnownSecrets(); registry.observeRequest(Map.of(), url);
            assertEquals("<redacted>", registry.snapshot().scrub(secret));
            assertEquals("https://example.invalid/?" + name + "=<redacted>&public=keep", new WirePrivacy(new KnownSecrets().snapshot(), false).text(url));
        }
        assertEquals("%ZZ+ A", PrivacyCatalog.decode("%ZZ%2B+%41", true));
    }
    @Test void incompleteUrlReplacementMarkersDoNotCauseSuffixRescanning() {
        var privacy = new WirePrivacy(new KnownSecrets().snapshot(), false);
        String text = "http://a<omitted: ".repeat(16384);
        assertTimeout(java.time.Duration.ofSeconds(2), () -> assertEquals(text, privacy.text(text)));
    }
    @Test void catalogBudgetsBoundFlatPathsAndQueryPairAllocation() {
        var privacy = new WirePrivacy(new KnownSecrets().snapshot(), false);
        assertEquals("<redacted>", privacy.clean("part.".repeat(10000) + "password", "PRIVATE")); assertTrue(privacy.failed);
        var query = new KnownSecrets(); query.observeRequest(Map.of(), "http://127.0.0.1/?" + "a=&".repeat(17000));
        assertTrue(query.snapshot().unavailable());
        var text = new WirePrivacy(new KnownSecrets().snapshot(), false);
        assertEquals("<redacted>", text.text("https://example.invalid/?" + "a=&".repeat(17000))); assertTrue(text.failed);
    }
    @Test void parsedBodyArrayAndNestedCredentialAncestryScrubUnrelatedTelemetry() {
        String secret = "S6_ARRAY_CREDENTIAL_81973264";
        for (Object body : List.of(Map.of("passwd", secret), Map.of("token", List.of(secret)), Map.of("token", List.of(Map.of("max_tokens", List.of(secret)))), Map.of("provider_credentials[0].value", secret))) {
            var registry = new KnownSecrets(); registry.observeBody(body);
            assertEquals("later <redacted>", new WirePrivacy(registry.snapshot(), false).text("later " + secret));
            assertFalse(registry.snapshot().unavailable());
        }
    }
    @Test void flattenedImageHintsAreIndexedAndAppliedToEveryAttributeSurfaceWithoutMutation() throws Exception {
        String uri = "https://image.example/PRIVATE_IMAGE";
        var attrs = Attributes.builder().put("gen_ai.input.messages.0.content.0.type", "image_url")
            .put("gen_ai.input.messages.0.content.0.url", uri)
            .put("gen_ai.input.messages.0.content.1.type", "text").put("gen_ai.input.messages.0.content.1.text", "PUBLIC_TEXT")
            .put("parts[0].mimeType", "audio/pcm").put("parts[0].content.chunk", "AQIDBA==")
            .put("parts[1].content.chunk", "PUBLIC_ADJACENT")
            .put(AttributeKey.stringArrayKey("parts.2.type"), List.of("image_url")).put("parts.2.url", uri)
            .put(AttributeKey.stringArrayKey("parts.3.mime_type"), List.of("text/plain", "image/png")).put("parts.3.url", uri).build();
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setResource(Resource.create(attrs)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var context = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.getDefault());
            var span = provider.get("s6-sibling-hints").spanBuilder("s6-sibling-hints").setAllAttributes(attrs).addLink(context, attrs).startSpan();
            span.addEvent("hints", attrs); span.end();
            var original = memory.getFinishedSpanItems().get(0);
            var scope = io.opentelemetry.sdk.common.InstrumentationScopeInfo.builder("s6-sibling-hints").setAttributes(attrs).build();
            var data = (io.opentelemetry.sdk.trace.data.SpanData)java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{io.opentelemetry.sdk.trace.data.SpanData.class}, (proxy, method, args) -> method.getName().equals("getInstrumentationScopeInfo") ? scope : method.invoke(original, args));
            for (boolean hide : List.of(false, true)) {
                var config = AutoConfig.resolve(Map.of("hide_input_images", hide), Map.of());
                String json = WireCore.dump(new OtlpJsonExporter(config).serialize(data));
                assertEquals(!hide, json.contains(uri)); assertFalse(json.contains("AQIDBA=="));
                assertTrue(json.contains("PUBLIC_TEXT")); assertTrue(json.contains("PUBLIC_ADJACENT"));
            }
            assertEquals(attrs.asMap(), original.getAttributes().asMap()); assertEquals(attrs.asMap(), original.getResource().getAttributes().asMap());
            assertEquals(attrs.asMap(), original.getEvents().get(0).getAttributes().asMap()); assertEquals(attrs.asMap(), original.getLinks().get(0).getAttributes().asMap());
        }
    }
    @Test void mediaHintsNeverCallApplicationCollectionGetters() {
        var hostile = new AbstractList<String>() {
            @Override public int size() { fail("Must not call size"); return 0; }
            @Override public String get(int index) { fail("Must not call get"); return ""; }
        };
        var privacy = new WirePrivacy(new KnownSecrets().snapshot(), false);
        assertEquals(hostile.getClass().getName(), WireCore.map(privacy.clean(Map.of("mime_type", hostile))).get("mime_type"));
    }
    @Test void atomicCredentialSourcesFallBackWithoutMixingAndNeverLogValues() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "s6-pair-", ".toml");
        var output = new java.io.ByteArrayOutputStream(); var stderr = System.err;
        try (var captured = new java.io.PrintStream(output, true, StandardCharsets.UTF_8)) {
            Files.writeString(file, "api_key = \"FILE_PAIR_API_81973264\"\nsecret_key = \"FILE_PAIR_SECRET_81973264\"\nproject_name = \"file-project\"\n");
            System.setErr(captured);
            for (String half : List.of("FI_API_KEY", "FI_SECRET_KEY")) {
                var config = AutoConfig.resolve(Map.of("config_file", file.toString(), half, "EXPLICIT_HALF_81973264", "project_name", "code-project"), Map.of(half, "ENV_HALF_81973264"));
                assertEquals("FILE_PAIR_API_81973264", config.get("FI_API_KEY")); assertEquals("FILE_PAIR_SECRET_81973264", config.get("FI_SECRET_KEY"));
                assertEquals("code-project", config.get("FI_PROJECT_NAME")); assertTrue(config.credentials());
                var registry = new KnownSecrets(); registry.seed(config, Map.of());
                assertEquals("<redacted> <redacted>", registry.snapshot().scrub("EXPLICIT_HALF_81973264 ENV_HALF_81973264"));
            }
            assertEquals(4, output.toString(StandardCharsets.UTF_8).lines().filter(line -> line.contains("Ignoring incomplete credentials")).count());
            assertTrue(output.toString(StandardCharsets.UTF_8).contains("missing FI_API_KEY")); assertTrue(output.toString(StandardCharsets.UTF_8).contains("missing FI_SECRET_KEY"));
            assertFalse(output.toString(StandardCharsets.UTF_8).contains("81973264"));
            var env = Map.of("FI_API_KEY", "ENV_PAIR_API", "FI_SECRET_KEY", "ENV_PAIR_SECRET");
            var fromEnv = AutoConfig.resolve(Map.of("config_file", file.toString(), "api_key", "HALF"), env);
            assertEquals("ENV_PAIR_API", fromEnv.get("FI_API_KEY")); assertEquals("ENV_PAIR_SECRET", fromEnv.get("FI_SECRET_KEY"));
            var fromCode = AutoConfig.resolve(Map.of("config_file", file.toString(), "api_key", "CODE_PAIR_API", "secret_key", "CODE_PAIR_SECRET"), env);
            assertEquals("CODE_PAIR_API", fromCode.get("FI_API_KEY")); assertEquals("CODE_PAIR_SECRET", fromCode.get("FI_SECRET_KEY"));
            var mixed = AutoConfig.resolve(Map.of("config_file", "target/no-s6-config", "api_key", "CODE_HALF"), Map.of("FI_SECRET_KEY", "ENV_OTHER_HALF"));
            assertFalse(mixed.credentials()); assertNull(mixed.get("FI_API_KEY")); assertNull(mixed.get("FI_SECRET_KEY"));
            Files.writeString(file, "secret_key = \"FILE_HALF\"\n");
            var incompleteFile = AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of("FI_API_KEY", "ENV_HALF"));
            assertFalse(incompleteFile.credentials());
            Files.writeString(file, "api_key = \"FILE_PAIR_API\"\nsecret_key = \"FILE_PAIR_SECRET\"\n");
            var blank = AutoConfig.resolve(Map.of("config_file", file.toString(), "api_key", " ", "secret_key", "CODE_PARTIAL"), Map.of());
            assertEquals("FILE_PAIR_API", blank.get("FI_API_KEY")); assertEquals("FILE_PAIR_SECRET", blank.get("FI_SECRET_KEY"));
        } finally { System.setErr(stderr); Files.deleteIfExists(file); }
    }
    @Test void fiProfilePairsAreAtomicWithinEachFileAndLowestPrecedence() throws Exception {
        Path root = Files.createTempDirectory(Path.of("target"), "s6-profiles-").toAbsolutePath();
        Path user = root.resolve("xdg/fi/config.toml"), project = root.resolve(".fi/config.toml"), file = root.resolve("futureagi.toml");
        var output = new java.io.ByteArrayOutputStream(); var stderr = System.err;
        try (var captured = new java.io.PrintStream(output, true, StandardCharsets.UTF_8)) {
            System.setErr(captured); Files.createDirectories(user.getParent()); Files.createDirectories(project.getParent());
            var environment = Map.of("XDG_CONFIG_HOME", root.resolve("xdg").toString(), "FI_API_KEY", "ENV_HALF_81973264");
            var options = Map.of("config_file", file.toString(), "api_key", "CODE_HALF_81973264");
            Files.writeString(user, "[profiles.default]\napi_key = 'USER_API_81973264'\nsecret_key = 'USER_SECRET_81973264'\nbase_url = 'http://127.0.0.1:1'\n[profiles.named]\napi_key = \"NAMED_API_81973264\"\nsecret_key = \"NAMED_SECRET_81973264\"\n");
            Files.writeString(project, "[profiles.default]\napi_key = \"PROJECT_HALF_81973264\"\n");
            Files.writeString(file, "api_key = \"FILE_HALF_81973264\"\n");
            AutoConfig resolved = AutoConfig.resolve(options, environment, root.resolve("app/deeper"));
            assertEquals("USER_API_81973264", resolved.get("FI_API_KEY")); assertEquals("USER_SECRET_81973264", resolved.get("FI_SECRET_KEY"));
            assertEquals("http://127.0.0.1:1", resolved.get("FI_BASE_URL"));
            var registry = new KnownSecrets(); registry.seed(resolved, Map.of());
            for (String ignored : List.of("PROJECT_HALF_81973264", "FILE_HALF_81973264", "ENV_HALF_81973264", "CODE_HALF_81973264")) assertEquals("<redacted>", registry.snapshot().scrub(ignored));
            Files.writeString(project, "[profiles.default]\napi_key = \"PROJECT_API\"\nsecret_key = \"PROJECT_SECRET\"\n");
            resolved = AutoConfig.resolve(options, environment, root);
            assertEquals("PROJECT_API", resolved.get("FI_API_KEY")); assertEquals("PROJECT_SECRET", resolved.get("FI_SECRET_KEY"));
            Files.writeString(file, "api_key = \"FILE_API\"\nsecret_key = \"FILE_SECRET\"\n");
            resolved = AutoConfig.resolve(options, environment, root);
            assertEquals("FILE_API", resolved.get("FI_API_KEY")); assertEquals("FILE_SECRET", resolved.get("FI_SECRET_KEY"));
            var named = AutoConfig.resolve(Map.of("config_file", "target/no-s6-config", "profile", "named"), Map.of("XDG_CONFIG_HOME", root.resolve("xdg").toString()), root);
            assertEquals("NAMED_API_81973264", named.get("FI_API_KEY")); assertEquals("NAMED_SECRET_81973264", named.get("FI_SECRET_KEY"));
            Files.writeString(user, "[profiles.default]\nsecret_key = \"USER_HALF\"\n");
            Files.writeString(project, "[profiles.default]\napi_key = \"PROJECT_HALF\"\n"); Files.delete(file);
            resolved = AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of("XDG_CONFIG_HOME", root.resolve("xdg").toString()), root);
            assertFalse(resolved.credentials()); assertNull(resolved.get("FI_API_KEY")); assertNull(resolved.get("FI_SECRET_KEY"));
            assertFalse(output.toString(StandardCharsets.UTF_8).contains("81973264"));
        } finally {
            System.setErr(stderr);
            try (var paths = Files.walk(root)) { for (Path path : paths.sorted(Comparator.reverseOrder()).toList()) Files.delete(path); }
        }
    }
    @Test void diagnosticsSanitizeCollectorUrlAfterStructuralCleaning() {
        String api = "S6_DIAGNOSTIC_API_81973264", secret = "S6_DIAGNOSTIC_SECRET_81973264";
        String ignored = "S6_DIAGNOSTIC_IGNORED_81973264", registered = "S6_DIAGNOSTIC_REGISTERED_81973264";
        TraceAIAuto.registerSecret(registered);
        String url = "http://USERINFO_PRIVATE:USERINFO_PASSWORD@127.0.0.1:1/public/" + api + "/" + secret + "/" + ignored + "/" + registered + "?token=QUERY_PRIVATE#FRAGMENT_PRIVATE";
        var config = AutoConfig.resolve(Map.of("config_file", "target/no-s6-config", "api_key", api, "secret_key", secret, "base_url", url), Map.of("FI_API_KEY", ignored));
        assertEquals("http://127.0.0.1:1/public/<redacted>/<redacted>/<redacted>/<redacted>", config.masked().get("FI_BASE_URL"));
        assertEquals(url, config.get("FI_BASE_URL"), "diagnostics must not mutate the exporter endpoint");
        assertEquals("<masked>", config.masked().get("FI_API_KEY")); assertEquals("<masked>", config.masked().get("FI_SECRET_KEY"));
        assertEquals("http://127.0.0.1:1/public", AutoConfig.resolve(Map.of("base_url", "http://127.0.0.1:1/public?token=QUERY_PRIVATE#FRAGMENT_PRIVATE"), Map.of()).masked().get("FI_BASE_URL"));
        assertEquals("<invalid URL>", AutoConfig.resolve(Map.of("base_url", "http://[invalid"), Map.of()).masked().get("FI_BASE_URL"));
    }
    @Test void commentScannerRespectsBasicAndLiteralStringBoundaries() {
        for (String assignment : List.of("api_key = 'SIMPLE#API_81973264'", "api_key = 'LITERAL\\#API_81973264'", "api_key = 'LITERAL_END\\'", "api_key = 'LITERAL\"#API_81973264'", "api_key = \"BASIC'#API_81973264\"", "api_key = \"BASIC\\\"#API_81973264\"", "api_key = \"BASIC_END\\\\\"")) {
            assertEquals(assignment, AutoConfig.stripComment(assignment), assignment);
            assertEquals(assignment + " ", AutoConfig.stripComment(assignment + " # ignored \"comment'"), assignment);
        }
        assertEquals("", AutoConfig.stripComment("# full line"));
        assertEquals("sample_rate = 1 ", AutoConfig.stripComment("sample_rate = 1 # comment"));
    }
    @Test void literalProfileCredentialsPreserveHashesAndAtomicFallback() throws Exception {
        Path root = Files.createTempDirectory(Path.of("target"), "s6-literal-profile-").toAbsolutePath();
        Path user = root.resolve("xdg/fi/config.toml"), file = root.resolve("futureagi.toml");
        try {
            Files.createDirectories(user.getParent());
            Files.writeString(user, "[profiles.default]\napi_key = 'SIMPLE#API_81973264' # API comment\nsecret_key = 'SIMPLE#SECRET_81973264' # secret comment\nbase_url = 'http://127.0.0.1:1' # endpoint comment\nproject = 'LITERAL\"#PROJECT\\' # project comment\n");
            var environment = Map.of("XDG_CONFIG_HOME", root.resolve("xdg").toString());
            var config = AutoConfig.resolve(Map.of("config_file", file.toString()), environment, root);
            assertTrue(config.credentials()); assertEquals("SIMPLE#API_81973264", config.get("FI_API_KEY")); assertEquals("SIMPLE#SECRET_81973264", config.get("FI_SECRET_KEY"));
            assertEquals("LITERAL\"#PROJECT\\", config.get("FI_PROJECT_NAME")); assertEquals("http://127.0.0.1:1", config.get("FI_BASE_URL"));
            Files.writeString(file, "api_key = \"FILE_PAIR_API_81973264\"\nsecret_key = \"FILE_PAIR_SECRET_81973264\"\n");
            var fallback = AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of("XDG_CONFIG_HOME", root.resolve("xdg").toString(), "FI_API_KEY", "ENV_HALF_81973264"), root);
            assertEquals("FILE_PAIR_API_81973264", fallback.get("FI_API_KEY")); assertEquals("FILE_PAIR_SECRET_81973264", fallback.get("FI_SECRET_KEY"));
            var registry = new KnownSecrets(); registry.seed(fallback, Map.of());
            assertEquals("<redacted> <redacted>", registry.snapshot().scrub("SIMPLE#API_81973264 SIMPLE#SECRET_81973264"));
        } finally {
            try (var paths = Files.walk(root)) { for (Path path : paths.sorted(Comparator.reverseOrder()).toList()) Files.delete(path); }
        }
    }
    @Test void registeredReplacementLikeSecretsDoNotRewriteExistingRedactionMarkers() {
        var registry = new KnownSecrets(); registry.register("redacted"); registry.register("PRIVATE_SECRET_81973264");
        String result = registry.snapshot().scrub("<redacted> redacted PRIVATE_SECRET_81973264");
        assertEquals("<redacted> <redacted> <redacted>", result); assertEquals(result, registry.snapshot().scrub(result));
    }
    @Test void a8InclusiveAlgebraNormalizesMergedCumulativeUsageOnce() {
        var anthropic = new WireCore.Parsed();
        WireCore.anthropicUsage(anthropic, WireCore.parse("{\"input_tokens\":15,\"output_tokens\":0,\"cache_read_input_tokens\":100,\"cache_creation_input_tokens\":20}"));
        WireCore.anthropicUsage(anthropic, WireCore.parse("{\"output_tokens\":4}")); WireCore.normalizeUsage(anthropic);
        assertEquals(135L, anthropic.usage.get("input")); assertEquals(139L, anthropic.usage.get("total"));
        var converse = new WireCore.Parsed();
        WireCore.usageSnapshot(converse, "bedrock.converse", WireCore.parse("{\"inputTokens\":8,\"outputTokens\":3,\"totalTokens\":11,\"cacheReadInputTokens\":100,\"cacheWriteInputTokens\":20}")); WireCore.normalizeUsage(converse);
        assertEquals(128L, converse.usage.get("input")); assertEquals(131L, converse.usage.get("total")); assertEquals(11L, converse.usage.get("provider_total"));
        var gemini = new WireCore.Parsed();
        WireCore.usageSnapshot(gemini, "gemini", WireCore.parse("{\"promptTokenCount\":12,\"toolUsePromptTokenCount\":3,\"candidatesTokenCount\":1,\"thoughtsTokenCount\":2}"));
        WireCore.usageSnapshot(gemini, "gemini", WireCore.parse("{\"candidatesTokenCount\":5,\"thoughtsTokenCount\":8,\"totalTokenCount\":33}")); WireCore.normalizeUsage(gemini);
        assertEquals(15L, gemini.usage.get("input")); assertEquals(13L, gemini.usage.get("output")); assertEquals(28L, gemini.usage.get("total")); assertEquals(33L, gemini.usage.get("provider_total"));
        var openai = new WireCore.Parsed();
        WireCore.openaiUsage(openai, WireCore.parse("{\"input_tokens\":20,\"output_tokens\":70,\"input_tokens_details\":{\"cached_tokens\":10},\"output_tokens_details\":{\"reasoning_tokens\":64}}"));
        WireCore.openaiUsage(openai, WireCore.parse("{\"input_tokens_details\":{\"audio_tokens\":3}}")); WireCore.normalizeUsage(openai);
        assertEquals(20L, openai.usage.get("input")); assertEquals(70L, openai.usage.get("output")); assertEquals(10L, openai.usage.get("cache_read")); assertEquals(90L, openai.usage.get("total"));
    }
    @Test void a8ResponseStreamsMergePartialDetailsBeforeNormalization() {
        var p = new WireCore.Parsed();
        var events = List.of(
            WireCore.map(WireCore.parse("{\"type\":\"response.created\",\"response\":{\"usage\":{\"input_tokens\":20,\"input_tokens_details\":{\"cached_tokens\":10}}}}")),
            WireCore.map(WireCore.parse("{\"type\":\"response.in_progress\",\"response\":{\"usage\":{\"input_tokens_details\":{\"audio_tokens\":3}}}}")),
            WireCore.map(WireCore.parse("{\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"usage\":{\"output_tokens\":70,\"total_tokens\":90,\"output_tokens_details\":{\"reasoning_tokens\":64}}}}")));
        WireCore.response(new WireCore.Body("sse", null, events, null), p, WireCore.match("POST", "http://127.0.0.1/v1/responses"));
        WireCore.normalizeUsage(p);
        assertEquals(20L, p.usage.get("input")); assertEquals(70L, p.usage.get("output")); assertEquals(90L, p.usage.get("total"));
        assertEquals(10L, p.usage.get("cache_read")); assertEquals(3L, p.usage.get("input_audio")); assertEquals(64L, p.usage.get("reasoning"));
    }
    @Test void a8ExplicitNullAbsentAndZeroAreNotConflated() {
        var p = new WireCore.Parsed();
        WireCore.anthropicUsage(p, WireCore.parse("{\"input_tokens\":5,\"output_tokens\":0,\"cache_read_input_tokens\":10}"));
        WireCore.anthropicUsage(p, WireCore.parse("{\"input_tokens\":null}")); WireCore.normalizeUsage(p);
        assertFalse(p.usage.containsKey("input")); assertFalse(p.usage.containsKey("total")); assertEquals(0L, p.usage.get("output")); assertEquals(10L, p.usage.get("cache_read"));
        p = new WireCore.Parsed(); WireCore.openaiUsage(p, WireCore.parse("{\"input_tokens\":0,\"output_tokens\":0,\"total_tokens\":100}")); WireCore.normalizeUsage(p);
        assertEquals(0L, p.usage.get("total")); assertEquals(100L, p.usage.get("provider_total"));
        p = new WireCore.Parsed(); WireCore.usageSnapshot(p, "gemini", WireCore.parse("{\"toolUsePromptTokenCount\":3,\"candidatesTokenCount\":0}")); WireCore.normalizeUsage(p);
        assertFalse(p.usage.containsKey("input")); assertFalse(p.usage.containsKey("total"));
    }
}
