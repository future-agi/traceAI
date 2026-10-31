package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.io.ByteArrayOutputStream;
import java.net.http.HttpHeaders;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundTwoRegressionTest {
    @Test void falseMultipartDelimitersRemainFilePayloadAtEveryChunkBoundary() {
        for (String suffix : List.of("X", "\rX", "-X", "\r\r\n")) {
            String file = "audio\r\n--b" + suffix + "\r\nContent-Disposition: form-data; name=prompt\r\n\r\nFILE_PAYLOAD_SHOULD_NOT_BE_RECORDED";
            String body = "--b\r\nContent-Disposition: form-data; name=model\r\n\r\nwhisper-1\r\n--b\r\nContent-Disposition: form-data; name=file; filename=a.wav\r\nContent-Type: audio/wav\r\n\r\n" + file + "\r\n--b--\r\n";
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            for (int split = 0; split <= bytes.length; split++) {
                BodyCapture capture = new BodyCapture("multipart/form-data; boundary=b", 16, false);
                capture.add(ByteBuffer.wrap(bytes, 0, split)); capture.add(ByteBuffer.wrap(bytes, split, bytes.length - split));
                assertEquals(Map.of("model", "whisper-1"), capture.multipart.fields);
                assertEquals(file.length(), capture.multipart.fileBytes); assertEquals(1, capture.multipart.files.size());
                assertEquals(file.length(), capture.multipart.files.get(0).size());
                assertFalse(capture.multipart.header.toString(StandardCharsets.UTF_8).contains("FILE_PAYLOAD"));
            }
        }
    }
    @Test void providerRulesHaveGlobalSpecificityAndAllRulesStillApply() {
        assertEquals("deepgram", WireCore.match("POST", "https://api.deepgram.com:11434/v1/listen").provider());
        assertEquals("azure.ai.openai", WireCore.match("POST", "https://x.openai.azure.com:11434/v1/chat/completions").provider());
        assertEquals("aws.bedrock", WireCore.match("POST", "https://bedrock-runtime.us-east-1.amazonaws.com:11434/model/m/invoke").provider());
        assertNull(WireCore.match("POST", "https://bedrock-runtime.invalid/model/m/invoke"));
        assertNull(WireCore.match("POST", "https://unrelated.amazonaws.com/model/m/invoke"));
        assertEquals("ollama", WireCore.match("POST", "http://localhost:11434/api/chat").provider());
    }
    @Test void malformedJsonCannotValidateAnyJsonBedrockRoutes() {
        for (String json : List.of("{unquoted: 'not JSON'}", "{'quoted':'single'}", "{\"x\":NaN}", "{\"x\":01}", "{\"x\":1,}", "[1,]", "/*comment*/{}", "{} {}", "true trailing", "{\"x\":\"bad\ncontrol\"}")) {
            var request = new WireCore.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke");
            request.body = json.getBytes(StandardCharsets.UTF_8);
            assertNull(AdversarialTest.build(request, AdversarialTest.response("application/json", "{}")), json);
        }
        assertNotNull(WireCore.parse("{\"escaped\":\"\\n\",\"n\":-1.2e3}"));
    }
    @Test void repeatedEncodingHeadersDecodeInBothTransports() throws Exception {
        byte[] json = "{\"choices\":[{\"message\":{\"content\":\"decoded\"}}]}".getBytes(StandardCharsets.UTF_8);
        var gzip = new ByteArrayOutputStream(); try (var out = new java.util.zip.GZIPOutputStream(gzip)) { out.write(json); }
        var deflate = new ByteArrayOutputStream(); try (var out = new java.util.zip.DeflaterOutputStream(deflate)) { out.write(gzip.toByteArray()); }
        var okhttp = new okhttp3.Headers.Builder().add("Content-Encoding", "gzip").add("Content-Encoding", "deflate").build();
        for (var headers : List.of(JdkTransport.headers(HttpHeaders.of(Map.of("Content-Encoding", List.of("gzip", "deflate")), (k, v) -> true)), OkHttpTransport.headers(okhttp), Map.of("Content-Encoding", "gzip, deflate"))) {
            var response = AdversarialTest.response("application/json", ""); response.body = deflate.toByteArray(); response.headers.putAll(headers);
            var result = AdversarialTest.build(AdversarialTest.chat("api.openai.com"), response);
            assertEquals("decoded", result.attributes().get("output.value")); assertFalse(result.attributes().containsKey("traceai.wire.undecoded_encoding"));
        }
    }
    @Test void streamingEndpointFlagDoesNotDependOnBodyFraming() {
        for (String url : List.of("https://generativelanguage.googleapis.com/v1/models/m:streamGenerateContent", "https://aiplatform.googleapis.com/publishers/anthropic/models/m:streamRawPredict", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse-stream", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke-with-response-stream")) {
            var req = AdversarialTest.request(url, Map.of("messages", List.of(), "contents", List.of()));
            var resp = AdversarialTest.response("application/json", "[]"); resp.firstChunkSeconds = .125;
            var result = AdversarialTest.build(req, resp); assertNotNull(result, url);
            assertEquals(true, result.attributes().get("gen_ai.request.stream"), url); assertEquals(.125, result.attributes().get("gen_ai.response.time_to_first_chunk"));
        }
    }
    @Test void messageTextPartsJoinWithNewlinesButStreamDeltasDoNot() {
        var parts = List.of(Map.of("type", "text", "text", "a"), Map.of("type", "image", "text", "skip"), Map.of("type", "text", "text", "b"));
        assertEquals("a\nb", WireCore.openaiMessage(Map.of("content", parts)).content);
        assertEquals("a\nb", WireCore.anthropicMessage("assistant", parts).content);
        assertEquals("a\nb", WireCore.bedrockMessage(Map.of("content", List.of(Map.of("text", "a"), Map.of("image", "skip"), Map.of("text", "b")))).content);
        assertEquals("a\nb", WireCore.geminiMessage(Map.of("parts", List.of(Map.of("text", "a"), Map.of("text", "b")))).content);
        var response = AdversarialTest.response("text/event-stream", TransportSafetyTest.chunk("a") + TransportSafetyTest.chunk("b"));
        assertEquals("ab", AdversarialTest.build(AdversarialTest.chat("api.openai.com"), response).attributes().get("output.value"));
    }
    @Test void cohereV1NdjsonAndV2ToolDeltasPreserveMetadataAndArguments() {
        var request = AdversarialTest.request("https://api.cohere.com/v2/chat", Map.of("messages", List.of()));
        String v1 = "{\"event_type\":\"text-generation\",\"text\":\"Hel\"}\n{\"event_type\":\"text-generation\",\"text\":\"lo\"}\n{\"event_type\":\"stream-end\",\"finish_reason\":\"COMPLETE\",\"response\":{\"generation_id\":\"id\",\"meta\":{\"billed_units\":{\"input_tokens\":2,\"output_tokens\":3}}}}\n";
        var result = AdversarialTest.build(request, AdversarialTest.response("application/x-ndjson", v1));
        assertEquals("Hello", result.attributes().get("output.value")); assertEquals("id", result.attributes().get("gen_ai.response.id")); assertEquals(5L, result.attributes().get("gen_ai.usage.total_tokens"));
        String stream = "data: {\"type\":\"tool-call-start\",\"index\":0,\"delta\":{\"message\":{\"tool_calls\":{\"id\":\"call\",\"function\":{\"name\":\"weather\",\"arguments\":\"{\"}}}}}\n\ndata: {\"type\":\"tool-call-delta\",\"index\":0,\"delta\":{\"message\":{\"tool_calls\":{\"function\":{\"arguments\":\"} rollback\"}}}}}\n\n";
        result = AdversarialTest.build(request, AdversarialTest.response("text/event-stream", stream));
        assertEquals("weather", result.attributes().get("gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"));
        assertEquals("<redacted>", result.attributes().get("gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"), "malformed JSON arguments must fail closed");
    }
    static SpanData originalSpan() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().setResource(Resource.create(Attributes.of(AttributeKey.stringKey("service.name"), "real-application-service")))
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var linked = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.builder().put("vendor", "value").build());
            var span = provider.tracerBuilder("real-application-scope").setInstrumentationVersion("2.3.4").setSchemaUrl("https://schema.example/v1").build()
                .spanBuilder("library-call").addLink(linked, Attributes.of(AttributeKey.stringKey("api_key"), "LINK_PRIVATE_CREDENTIAL")).startSpan();
            span.setAttribute("gen_ai.span.kind", "LLM"); span.setAttribute("fi.raw_input", "PRIVATE_INPUT"); span.setAttribute("fi.raw_output", "PRIVATE_OUTPUT");
            span.setAttribute("input.value", "PRIVATE_INPUT"); span.setAttribute("output.value", "PRIVATE_OUTPUT"); span.setAttribute("password", "ATTRIBUTE_PRIVATE_PASSWORD");
            span.setAttribute("media", "data:image/png;base64," + "AAAA".repeat(100));
            span.setStatus(StatusCode.ERROR, "echo CONFIG_PRIVATE_KEY https://x/?sig=SIGNED_PRIVATE");
            span.recordException(new IllegalStateException("echo CONFIG_PRIVATE_KEY https://x/?token=EVENT_PRIVATE_TOKEN"));
            span.addEvent("diagnostic", Attributes.of(AttributeKey.stringKey("api_key"), "EVENT_PRIVATE_KEY", AttributeKey.longKey("number"), 3L));
            span.end(); return exporter.getFinishedSpanItems().get(0);
        }
    }
    @Test void forwardedSpansPreserveEventsLinksResourceAndFullScope() throws Exception {
        SpanData original = originalSpan(); var config = AutoConfig.resolve(Map.of("api_key", "CONFIG_PRIVATE_KEY"), Map.of());
        var forwarded = ApplicationOtel.snapshot(original, getClass().getClassLoader(), config, TraceAIAuto.resource(config));
        assertEquals(2, forwarded.getEvents().size()); assertEquals(1, forwarded.getLinks().size());
        assertEquals(original.getEvents().get(0).getEpochNanos(), forwarded.getEvents().get(0).getEpochNanos());
        assertEquals(original.getLinks().get(0).getSpanContext(), forwarded.getLinks().get(0).getSpanContext());
        assertEquals("real-application-service", forwarded.getResource().getAttributes().get(AttributeKey.stringKey("service.name")));
        assertEquals("real-application-scope", forwarded.getInstrumentationScopeInfo().getName()); assertEquals("2.3.4", forwarded.getInstrumentationScopeInfo().getVersion());
        assertEquals("https://schema.example/v1", forwarded.getInstrumentationScopeInfo().getSchemaUrl());
        String json = WireCore.dump(new OtlpJsonExporter(config).serialize(forwarded));
        for (String secret : List.of("CONFIG_PRIVATE_KEY", "LINK_PRIVATE_CREDENTIAL", "ATTRIBUTE_PRIVATE_PASSWORD", "EVENT_PRIVATE_TOKEN", "EVENT_PRIVATE_KEY", "SIGNED_PRIVATE", "AAAA".repeat(100))) assertFalse(json.contains(secret), secret);
        var scope = WireCore.get(WireCore.first(WireCore.get(WireCore.parse(json), "scopeSpans")), "scope");
        assertEquals("real-application-scope", WireCore.get(scope, "name")); assertEquals("2.3.4", WireCore.get(scope, "version"));
        assertEquals("PRIVATE_INPUT", original.getAttributes().get(AttributeKey.stringKey("fi.raw_input")));
        assertTrue(original.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.message")).contains("CONFIG_PRIVATE_KEY"));
    }
    @Test void exportBoundaryMasksOwnAndForwardedLibrarySpansIncludingStatusAndEvents() throws Exception {
        SpanData original = originalSpan();
        for (String flag : List.of("hide_inputs", "hide_input_text", "hide_input_messages", "hide_outputs", "hide_output_text", "hide_output_messages")) {
            var config = AutoConfig.resolve(Map.of(flag, true, "api_key", "CONFIG_PRIVATE_KEY"), Map.of());
            for (SpanData data : List.of(original, ApplicationOtel.snapshot(original, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
                String json = WireCore.dump(new OtlpJsonExporter(config).serialize(data));
                assertFalse(json.contains(flag.contains("output") ? "PRIVATE_OUTPUT" : "PRIVATE_INPUT"), flag);
                assertFalse(json.contains("exception.message")); assertFalse(json.contains("exception.stacktrace"));
                assertFalse(json.contains("CONFIG_PRIVATE_KEY")); assertFalse(json.contains("SIGNED_PRIVATE"));
            }
        }
    }
}
