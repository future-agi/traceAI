package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundFourRegressionTest {
    @Test void legacyWrapperInputNamespacesAreHiddenAtBothExportBoundaries() throws Exception {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("existing-wrapper").spanBuilder("embedding").startSpan();
            for (String key : List.of("embedding.input_text", "embedding.input_text.0", "embedding.input_text.12", "gen_ai.reranker.query", "input.value")) span.setAttribute(key, "PRIVATE_INPUT_" + key);
            span.setAttribute("gen_ai.reranker.model", "model"); span.setAttribute("gen_ai.reranker.input_documents", 3L);
            span.setAttribute("gen_ai.embeddings.dimension.count", 4L); span.setAttribute("output.value", "public output");
            span.setStatus(StatusCode.ERROR, "PRIVATE_INPUT_status");
            span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.message"), "PRIVATE_INPUT_event"));
            span.end(); var original = memory.getFinishedSpanItems().get(0);
            for (String flag : List.of("hide_inputs", "hide_input_text", "hide_input_messages")) {
                var config = AutoConfig.resolve(Map.of(flag, true), Map.of());
                var forwarded = ApplicationOtel.snapshot(original, getClass().getClassLoader(), config, TraceAIAuto.resource(config));
                for (var data : List.of(original, forwarded)) {
                    var exported = new OtlpJsonExporter(config).serialize(data);
                    String json = WireCore.dump(exported);
                    assertFalse(json.contains("PRIVATE_INPUT"), flag);
                    assertTrue(json.contains("public output")); assertTrue(json.contains("gen_ai.reranker.model"));
                    assertTrue(json.contains("gen_ai.reranker.input_documents")); assertTrue(json.contains("gen_ai.embeddings.dimension.count"));
                }
            }
            assertEquals("PRIVATE_INPUT_embedding.input_text.0", original.getAttributes().get(AttributeKey.stringKey("embedding.input_text.0")));
            assertEquals("PRIVATE_INPUT_gen_ai.reranker.query", original.getAttributes().get(AttributeKey.stringKey("gen_ai.reranker.query")));
            assertEquals("PRIVATE_INPUT_status", original.getStatus().getDescription());
            assertEquals("PRIVATE_INPUT_event", original.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.message")));
        }
    }
    @Test void libraryAttributesStatusAndExceptionEventsApplyPrivacyWithoutHideFlags() throws Exception {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            String text = "{\"password\":\"PRIVATE_PASSWORD\",\"url\":\"https://x/?sig=PRIVATE_SIGNATURE\",\"media\":\"data:audio/wav;base64,QUJDRA==\",\"echo\":\"CONFIGURED_PRIVATE_KEY\"}";
            var span = provider.get("library").spanBuilder("call").startSpan();
            span.setAttribute("input.value", text); span.setStatus(StatusCode.ERROR, text);
            span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.message"), text, AttributeKey.stringKey("exception.stacktrace"), text));
            span.end(); var original = memory.getFinishedSpanItems().get(0);
            var config = AutoConfig.resolve(Map.of("api_key", "CONFIGURED_PRIVATE_KEY"), Map.of());
            for (var data : List.of(original, ApplicationOtel.snapshot(original, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
                String json = WireCore.dump(new OtlpJsonExporter(config).serialize(data));
                for (String secret : List.of("PRIVATE_PASSWORD", "PRIVATE_SIGNATURE", "QUJDRA==", "CONFIGURED_PRIVATE_KEY")) assertFalse(json.contains(secret), secret);
                assertTrue(json.contains("<redacted>")); assertTrue(json.contains("<omitted: 8 base64 chars>"));
            }
            assertEquals(text, original.getAttributes().get(AttributeKey.stringKey("input.value")));
            assertEquals(text, original.getStatus().getDescription());
        }
    }
    @Test void filenameLessFilePartsAreCountOnlyAtEverySplitEvenOnHttpRejection() {
        String file = "PRIVATE_FILE_PAYLOAD";
        for (String mime : List.of("", "Content-Type: text/plain\r\n", "Content-Type: application/json\r\n")) {
            byte[] body = ("--b\r\nContent-Disposition: form-data; name=model\r\n\r\nwhisper-1\r\n--b\r\nContent-Disposition: form-data; name=file\r\n" + mime + "\r\n" + file + "\r\n--b--\r\n").getBytes(StandardCharsets.UTF_8);
            for (int split = 0; split <= body.length; split++) {
                var capture = new BodyCapture("multipart/form-data; boundary=b", 8, false);
                capture.add(ByteBuffer.wrap(body, 0, split)); capture.add(ByteBuffer.wrap(body, split, body.length - split)); capture.body();
                assertEquals(Map.of("model", "whisper-1"), capture.multipart.fields);
                assertEquals(file.length(), capture.multipart.fileBytes); assertEquals(1, capture.multipart.files.size());
                assertEquals(file.length(), capture.multipart.files.get(0).size()); assertEquals(0, capture.multipart.field.size());
                var request = new WireCore.Request("POST", "https://api.openai.com/v1/audio/transcriptions");
                request.fields = capture.multipart.fields; request.files = capture.multipart.files; request.fileBytes = capture.multipart.fileBytes;
                for (int status : List.of(200, 400)) {
                    var response = AdversarialTest.response("application/json", status == 400 ? "{\"error\":{\"message\":\"rejected\"}}" : "{\"text\":\"ok\"}"); response.status = status;
                    var result = AdversarialTest.build(request, response); assertNotNull(result);
                    assertEquals(status == 200, result.ok()); assertEquals((long)file.length(), result.attributes().get("traceai.audio.input_bytes"));
                    assertFalse(WireCore.dump(result.attributes()).contains(file));
                }
            }
        }
    }
    @Test void incompleteFilenameLessFilePayloadIsNeverRetained() {
        var capture = new BodyCapture("multipart/form-data; boundary=b", 8, false);
        capture.add(ByteBuffer.wrap("--b\r\nContent-Disposition: form-data; name=file\r\nContent-Type: text/plain\r\n\r\nPRIVATE_FILE_PAYLOAD".getBytes(StandardCharsets.UTF_8)));
        assertEquals("PRIVATE_FILE_PAYLOAD".length(), capture.multipart.fileBytes); assertEquals(0, capture.multipart.field.size()); assertTrue(capture.multipart.fields.isEmpty());
    }
    @Test void oneMessageJoinsTextPartsWithNewlinesAcrossOpenaiAnthropicAndBedrock() {
        var parts = List.of(Map.of("type", "text", "text", "A"), Map.of("type", "image", "source", Map.of("url", "https://x/image")), Map.of("type", "text", "text", "B"));
        var input = Map.of("role", "user", "content", parts); var output = Map.of("role", "assistant", "content", parts);
        for (String url : List.of("https://api.openai.com/v1/chat/completions", "https://api.anthropic.com/v1/messages", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke")) {
            Object response = url.contains("openai") ? Map.of("choices", List.of(Map.of("message", output))) : output;
            var result = AdversarialTest.build(AdversarialTest.request(url, Map.of("messages", List.of(input))), AdversarialTest.response("application/json", WireCore.dump(response)));
            assertNotNull(result); assertEquals("A\nB", result.attributes().get("gen_ai.input.messages.0.message.content"));
            assertEquals("A\nB", result.attributes().get("gen_ai.output.messages.0.message.content")); assertEquals("A\nB", result.attributes().get("output.value"));
        }
        var blocks = List.of(Map.of("text", "A"), Map.of("image", Map.of("format", "png")), Map.of("text", "B"));
        var result = AdversarialTest.build(AdversarialTest.request("https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse", Map.of("messages", List.of(Map.of("role", "user", "content", blocks)))),
            AdversarialTest.response("application/json", WireCore.dump(Map.of("output", Map.of("message", Map.of("role", "assistant", "content", blocks))))));
        assertNotNull(result); assertEquals("A\nB", result.attributes().get("gen_ai.input.messages.0.message.content"));
        assertEquals("A\nB", result.attributes().get("output.value"));
    }
    @Test void transportProvenanceWalkIsBoundedCycleSafeAndNeverCallsOverrides() {
        var nativeError = new IOException("native");
        assertTrue(WireCapture.causedBy(new IOException("adapter", new IOException("adapter", nativeError)), nativeError));
        assertFalse(WireCapture.causedBy(new IOException("independent"), nativeError));
        var a = new IOException("a"); var b = new IOException("b"); a.initCause(b); b.initCause(a);
        assertFalse(WireCapture.causedBy(a, nativeError));
        var hostile = new IOException("application", nativeError) {
            @Override public synchronized Throwable getCause() { fail("Application override must not be invoked"); return nativeError; }
        };
        assertFalse(WireCapture.causedBy(hostile, nativeError)); assertTrue(WireCapture.causedBy(hostile, hostile));
        Throwable deep = nativeError; for (int i = 0; i < 100; i++) deep = new IOException("adapter", deep);
        assertFalse(WireCapture.causedBy(deep, nativeError));
    }
}
