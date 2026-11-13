package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.lang.reflect.Proxy;
import java.time.Duration;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundFourPrivacyTest {
    static SpanData original() {
        var memory = InMemorySpanExporter.create();
        var extras = Attributes.of(AttributeKey.doubleKey("metric"), Double.NaN, AttributeKey.stringKey("password"), "PRIVATE_SECRET");
        try (var provider = SdkTracerProvider.builder().setResource(Resource.create(extras)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var linked = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.builder().put("vendor", "PRIVATE_CONFIG_KEY").build());
            var span = provider.tracerBuilder("scope-PRIVATE_CONFIG_KEY").setInstrumentationVersion("version-PRIVATE_CONFIG_KEY").build().spanBuilder("name").addLink(linked, extras).startSpan();
            span.setAttribute("gen_ai.operation.name", "text_to_speech");
            span.setAttribute("metric-PRIVATE_CONFIG_KEY", 3L);
            span.setAttribute("input.value", "PRIVATE_INPUT"); span.setAttribute("gen_ai.system_instructions", "PRIVATE_SYSTEM");
            span.setAttribute("gen_ai.tool.custom", "PRIVATE_TOOL"); span.setAttribute("gen_ai.input.messages.0.message.content", "PRIVATE_INPUT");
            span.setAttribute("gen_ai.request.parameters", "{\"temperature\":0.7}"); span.setAttribute("gen_ai.audio.transcript", "PRIVATE_TRANSCRIPT");
            span.setAttribute("gen_ai.input.image.uri", "https://image.example/PRIVATE_IMAGE"); span.setAttribute("file.uri", "https://file.example/PRIVATE_FILE");
            span.setAttribute("number", Double.NEGATIVE_INFINITY); span.setAttribute(AttributeKey.doubleArrayKey("numbers"), List.of(1d, Double.NaN, Double.POSITIVE_INFINITY));
            span.setStatus(StatusCode.ERROR, "java.io.IOException: {\"password\":\"PRIVATE_DIAGNOSTIC\"}");
            span.addEvent("event-PRIVATE_CONFIG_KEY", extras); span.end();
            SpanData finished = memory.getFinishedSpanItems().get(0);
            var scope = io.opentelemetry.sdk.common.InstrumentationScopeInfo.builder("scope-PRIVATE_CONFIG_KEY").setVersion("version-PRIVATE_CONFIG_KEY").setAttributes(extras).build();
            return (SpanData)Proxy.newProxyInstance(RoundFourPrivacyTest.class.getClassLoader(), new Class<?>[]{SpanData.class}, (proxy, method, args) -> method.getName().equals("getInstrumentationScopeInfo") ? scope : method.invoke(finished, args));
        }
    }
    @Test void allSpanSurfacesAreSanitizedAndNonFiniteNumbersRemainSerializable() throws Exception {
        var original = original(); var config = AutoConfig.resolve(Map.of("api_key", "PRIVATE_CONFIG_KEY"), Map.of());
        for (SpanData data : List.of(original, ApplicationOtel.snapshot(original, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
            String json = WireCore.dump(new OtlpJsonExporter(config).serialize(data));
            assertFalse(json.contains("PRIVATE_CONFIG_KEY")); assertFalse(json.contains("PRIVATE_SECRET")); assertFalse(json.contains("PRIVATE_DIAGNOSTIC"));
            for (String value : List.of("NaN", "Infinity", "-Infinity")) assertTrue(json.contains(value));
            assertTrue(json.contains("events")); assertTrue(json.contains("links")); assertTrue(json.contains("scope-<redacted>"));
        }
        assertEquals("PRIVATE_CONFIG_KEY", original.getLinks().get(0).getSpanContext().getTraceState().get("vendor"));
        assertEquals("PRIVATE_INPUT", original.getAttributes().get(AttributeKey.stringKey("input.value")));
    }
    @Test void systemToolTranscriptInvocationAndImageFamiliesHonorTheirFlags() {
        var data = original();
        for (String flag : List.of("hide_inputs", "hide_input_text", "hide_input_messages")) {
            String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of(flag, true), Map.of())).serialize(data));
            for (String secret : List.of("PRIVATE_INPUT", "PRIVATE_SYSTEM", "PRIVATE_TOOL", "PRIVATE_TRANSCRIPT")) assertFalse(json.contains(secret), flag + ":" + secret);
        }
        String parameters = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of("hide_llm_invocation_parameters", true), Map.of())).serialize(data));
        assertFalse(parameters.contains("gen_ai.request.parameters"));
        String images = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of("hide_input_images", true), Map.of())).serialize(data));
        assertFalse(images.contains("PRIVATE_IMAGE")); assertFalse(images.contains("PRIVATE_FILE"));
    }
    @Test void legacyCohereContentFamiliesHonorEachSideWithoutMutatingApplicationSpans() throws Exception {
        var input = Attributes.of(AttributeKey.stringKey("cohere.preamble"), "PRIVATE_COHERE_SYSTEM_PROMPT");
        var output = Attributes.builder().put("message.tool_calls", "PRIVATE_COHERE_TOOL_ROOT")
            .put("message.tool_calls.0.tool_call.function.name", "PRIVATE_COHERE_TOOL_NAME")
            .put("message.tool_calls.0.tool_call.function.arguments", "{\"location\":\"PRIVATE_COHERE_TOOL_ARGUMENT\"}")
            .put(AttributeKey.stringArrayKey("message.tool_calls.12.tool_call.function.arguments"), List.of("PRIVATE_COHERE_TOOL_ARRAY")).build();
        var attributes = input.toBuilder().putAll(output).put("gen_ai.request.model", "public-model").build();
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setResource(Resource.create(attributes)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var linked = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.getDefault());
            var span = provider.get("existing-cohere-wrapper").spanBuilder("Cohere Chat").setAllAttributes(attributes).addLink(linked, attributes).startSpan();
            span.end(); var original = memory.getFinishedSpanItems().get(0);
            var scope = io.opentelemetry.sdk.common.InstrumentationScopeInfo.builder("existing-cohere-wrapper").setAttributes(attributes).build();
            var data = (SpanData)Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{SpanData.class}, (proxy, method, args) -> method.getName().equals("getInstrumentationScopeInfo") ? scope : method.invoke(original, args));
            for (String flag : List.of("none", "hide_inputs", "hide_input_text", "hide_input_messages", "hide_outputs", "hide_output_text", "hide_output_messages", "both")) {
                var config = AutoConfig.resolve(flag.equals("none") ? Map.of() : flag.equals("both") ? Map.of("hide_inputs", true, "hide_outputs", true) : Map.of(flag, true), Map.of());
                boolean hideInput = flag.startsWith("hide_input") || flag.equals("both"), hideOutput = flag.startsWith("hide_output") || flag.equals("both");
                for (var candidate : List.of(data, ApplicationOtel.snapshot(data, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
                    String json = WireCore.dump(new OtlpJsonExporter(config).serialize(candidate));
                    assertEquals(!hideInput, json.contains("PRIVATE_COHERE_SYSTEM_PROMPT"), flag);
                    for (String value : List.of("PRIVATE_COHERE_TOOL_ROOT", "PRIVATE_COHERE_TOOL_NAME", "PRIVATE_COHERE_TOOL_ARGUMENT", "PRIVATE_COHERE_TOOL_ARRAY")) assertEquals(!hideOutput, json.contains(value), flag + ":" + value);
                    assertTrue(json.contains("public-model")); assertTrue(json.contains("existing-cohere-wrapper")); assertTrue(json.contains("links"));
                }
            }
            assertEquals(attributes.asMap(), original.getAttributes().asMap()); assertEquals(attributes.asMap(), original.getResource().getAttributes().asMap());
            assertEquals(attributes.asMap(), original.getLinks().get(0).getAttributes().asMap());
        }
    }
    @Test void signedQueryScrubbingIsBoundedOnQuestionMarkPromptsAndRetainsUnsignedText() {
        var privacy = new WirePrivacy(Map.of(), null);
        assertTimeout(Duration.ofSeconds(1), () -> {
            for (String prompt : List.of("?".repeat(32768), "?a".repeat(16384), "?" + "a".repeat(32767))) {
                assertEquals(prompt, privacy.text(prompt));
                String json = WireCore.dump(Map.of("prompt", prompt));
                assertEquals(json, privacy.text(json));
            }
        });
    }
    @Test void signedQueryScrubbingKeepsAllCredentialNamesAndUrlDelimitersCovered() {
        var privacy = new WirePrivacy(Map.of(), null);
        for (String name : List.of("key", "api_key", "apikey", "token", "access_token", "sig", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-goog-signature", "x-goog-credential", "se", "sp", "sv", "sr", "skoid", "sktid", "API%5FKEY", "%73ig")) {
            String url = "https://x/?safe=keep&" + name + "=PRIVATE_SIGNED_VALUE?with=equals&safe=still#fragment";
            assertEquals("https://x/?safe=keep&" + name + "=<redacted>&safe=still#fragment", privacy.text(url), name);
        }
        // S6: apostrophes are legal URL characters, not query-value delimiters.
        assertEquals("prefix ?sig=<redacted>", privacy.text("prefix ?sig=PRIVATE_VALUE'suffix"));
        for (String delimiter : List.of(" ", "\t", "\n", "\"", "<", ">", "\\", "#")) {
            assertEquals("prefix ?sig=<redacted>" + delimiter + "suffix", privacy.text("prefix ?sig=PRIVATE_VALUE" + delimiter + "suffix"));
        }
        assertEquals("????sig=<redacted>", privacy.text("????sig=PRIVATE_VALUE"));
        // S6 parses one URL layer; a nested URL inside an ordinary query value is not decoded recursively.
        assertEquals("https://x/?next=https://y/?sig=PRIVATE_VALUE&safe=keep", privacy.text("https://x/?next=https://y/?sig=PRIVATE_VALUE&safe=keep"));
    }
    @Test void credentialHeaderNamesArePrivateWithoutPreviouslyObservingTheirValues() {
        var privacy = new WirePrivacy(Map.of(), null);
        HeaderPrivacyApp.HEADERS.forEach((header, value) -> {
            for (String spelling : List.of(header, header.toUpperCase(Locale.ROOT).replace('-', '_'))) {
                for (String prefix : List.of("", "http.request.header.", "http.response.header.")) {
                    String key = prefix + spelling;
                    assertEquals("<redacted>", privacy.clean(key, value), key);
                    assertEquals("<redacted>", privacy.clean(key, List.of(value, "second-private-value")), key);
                    String json = WireCore.dump(Map.of(key, value));
                    assertFalse(privacy.text(json).contains(value), key);
                    assertFalse(privacy.text("java.io.IOException: " + json).contains(value), key);
                    assertFalse(privacy.text("java.io.IOException: " + json.substring(0, json.length() - 2)).contains(value), key);
                }
            }
        });
    }
    @Test void forwardedCredentialHeaderAttributesAreSanitizedWithoutObservingTransport() throws Exception {
        var config = AutoConfig.resolve(Map.of(), Map.of());
        for (boolean arrays : List.of(false, true)) {
            var memory = InMemorySpanExporter.create();
            var attributes = HeaderPrivacyApp.attributes(arrays);
            try (var provider = SdkTracerProvider.builder().setResource(Resource.create(attributes)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
                var linked = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.getDefault());
                var span = provider.get("headers").spanBuilder("headers").addLink(linked, attributes).setAllAttributes(attributes).startSpan();
                span.addEvent("headers", attributes); span.end();
                var original = memory.getFinishedSpanItems().get(memory.getFinishedSpanItems().size() - 1);
                var scope = io.opentelemetry.sdk.common.InstrumentationScopeInfo.builder("headers").setAttributes(attributes).build();
                var data = (SpanData)Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{SpanData.class}, (proxy, method, args) -> method.getName().equals("getInstrumentationScopeInfo") ? scope : method.invoke(original, args));
                for (var candidate : List.of(data, ApplicationOtel.snapshot(data, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
                    String json = WireCore.dump(new OtlpJsonExporter(config).serialize(candidate));
                    for (String value : HeaderPrivacyApp.HEADERS.values()) assertFalse(json.contains(value), value);
                    assertFalse(json.contains("second-private-value"));
                    assertTrue(json.contains("http.request.header.x-goog-api-key")); assertTrue(json.contains("<redacted>"));
                }
                assertEquals(attributes.asMap(), original.getAttributes().asMap()); assertEquals(attributes.asMap(), original.getEvents().get(0).getAttributes().asMap());
                assertEquals(attributes.asMap(), original.getLinks().get(0).getAttributes().asMap()); assertEquals(attributes.asMap(), original.getResource().getAttributes().asMap());
            }
        }
    }
    @Test void diagnosticsEmbeddedJsonNeverLeaksCredentialOrMediaValues() {
        var privacy = new WirePrivacy(Map.of(), null);
        String stack = "java.io.IOException: {\"authorization_token\":\"PRIVATE_JSON_SECRET\",\"audio_base64\":\"" + "QUJD".repeat(100) + "\"}\n at application.Class.method(Class.java:1)";
        String clean = privacy.text(stack);
        assertFalse(clean.contains("PRIVATE_JSON_SECRET")); assertFalse(clean.contains("QUJD"));
        assertTrue(clean.contains("<redacted>")); assertTrue(clean.contains("<omitted: 400 base64 chars>"));
    }
    @Test void cyclicBranchingAndUnknownValuesAreBoundedWithoutCallingUserCode() {
        var privacy = new WirePrivacy(Map.of(), null);
        Object hostile = new Object() { @Override public String toString() { fail("Must not execute user toString"); return "secret"; } };
        assertEquals(hostile.getClass().getName(), privacy.clean(hostile));
        var hostileError = new java.io.IOException() { @Override public String getMessage() { fail("Must not invoke an application exception getter"); return "secret"; } };
        assertEquals(hostileError.getClass().getName(), WireCore.exceptionMessage(hostileError));
        Map<String, Object> hostileMap = new HashMap<>() { @Override public Set<Map.Entry<String, Object>> entrySet() { fail("Must not execute user Map getters"); return Set.of(); } };
        assertEquals(hostileMap.getClass().getName(), privacy.clean(hostileMap));
        var wrapped = Collections.unmodifiableMap(hostileMap);
        assertEquals(wrapped.getClass().getName(), privacy.clean(wrapped));
        var hostileList = new AbstractList<Object>() {
            public Object get(int index) { fail("Must not execute user list getters"); return null; }
            public int size() { fail("Must not execute user list size"); return 0; }
        };
        var wrappedList = Collections.unmodifiableList(hostileList);
        assertEquals(wrappedList.getClass().getName(), privacy.clean(wrappedList));
        List<Object> cycle = new ArrayList<>(); cycle.add(cycle);
        assertTimeout(Duration.ofSeconds(2), () -> assertTrue(WireCore.dump(privacy.clean(cycle)).contains("<redacted>")));
        Object branch = "leaf";
        for (int i = 0; i < 12; i++) branch = Collections.nCopies(16, branch);
        Object tree = branch;
        assertTimeout(Duration.ofSeconds(2), () -> assertTrue(WireCore.dump(privacy.clean(tree)).length() < 300000));
    }
    @Test void sanitizerFailureDropsContentButKeepsIdentityAndTiming() {
        SpanData original = original();
        SpanData bad = (SpanData)Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{SpanData.class}, (proxy, method, args) -> {
            if (method.getName().equals("getAttributes")) throw new IllegalStateException("synthetic sanitizer failure");
            return method.invoke(original, args);
        });
        String json = WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of(), Map.of())).serialize(bad));
        assertTrue(json.contains(original.getSpanId())); assertTrue(json.contains(Long.toString(original.getStartEpochNanos())));
        assertTrue(json.contains(Long.toString(original.getEndEpochNanos()))); assertFalse(json.contains("PRIVATE_"));
    }
}
