package ai.traceai.auto;

import org.junit.jupiter.api.Test;
import java.net.http.HttpHeaders;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class VerifierRegressionTest {
    @Test void multipartUnquotedAndCaseInsensitiveFilenamesNeverRetainFiles() {
        for (String parameter : List.of("filename=voice.wav", "FILENAME=voice.wav", "FILENAME = \"voice.wav\"", "filename=\"\"", "filename*=UTF-8''voice.wav", "")) {
            String body = "--b\r\nContent-Disposition: form-data; NAME=model\r\n\r\nwhisper-1\r\n--b\r\nContent-Disposition: form-data; name=\"file\"; " + parameter + "\r\nContent-Type: audio/wav\r\n\r\nWAV_PRIVATE_FILE_CONTENT\r\n--b--\r\n";
            BodyCapture capture = new BodyCapture("multipart/form-data; boundary=b", 8, false);
            for (byte b : body.getBytes(StandardCharsets.UTF_8)) capture.add(ByteBuffer.wrap(new byte[]{b}));
            assertEquals(Map.of("model", "whisper-1"), capture.multipart.fields, parameter);
            assertEquals(24, capture.multipart.fileBytes, parameter); assertEquals(1, capture.multipart.files.size());
            WireCore.Request request = new WireCore.Request("POST", "https://api.openai.com/v1/audio/transcriptions");
            request.fields = capture.multipart.fields; request.files = capture.multipart.files; request.fileBytes = capture.multipart.fileBytes;
            var result = AdversarialTest.build(request, AdversarialTest.response("application/json", "{\"text\":\"ok\"}"));
            assertNotNull(result); assertFalse(String.valueOf(result.attributes().get("input.value")).contains("WAV_PRIVATE"));
            assertEquals(24L, result.attributes().get("traceai.audio.input_bytes"));
        }
    }
    @Test void signedUrlsDecodeParameterNamesBeforeRedactingValues() {
        WirePrivacy privacy = new WirePrivacy(Map.of(), null);
        String url = "https://blob.example/a?api%5Fkey=URL_PRIVATE_TOKEN&%58-Amz-Signature=SIGNED_PRIVATE_TOKEN&%73ig=OTHER_PRIVATE_TOKEN&safe=keep#fragment";
        assertEquals("https://blob.example/a?api%5Fkey=<redacted>&%58-Amz-Signature=<redacted>&%73ig=<redacted>&safe=keep#fragment", privacy.text(url));
        assertEquals("https://x/?bad%=keep&sig=<redacted>", privacy.text("https://x/?bad%=keep&sig=private"));
    }
    @Test void repeatedCredentialHeadersRedactEachIndividualEcho() {
        Map<String, List<String>> raw = Map.of("Authorization", List.of("Token REQUEST_SECRET_111", "Bearer REQUEST_SECRET_222"), "api-key", List.of("REQUEST_SECRET_333", "REQUEST_SECRET_444"));
        okhttp3.Headers.Builder okhttp = new okhttp3.Headers.Builder(); raw.forEach((key, values) -> values.forEach(value -> okhttp.add(key, value)));
        for (Map<String, String> headers : List.of(JdkTransport.headers(HttpHeaders.of(raw, (key, value) -> true)), OkHttpTransport.headers(okhttp.build()))) {
            var request = AdversarialTest.chat("api.openai.com"); request.headers.putAll(headers);
            var result = AdversarialTest.build(request, errorResponse("echo REQUEST_SECRET_111 REQUEST_SECRET_222 REQUEST_SECRET_333 REQUEST_SECRET_444"));
            assertEquals("echo <redacted> <redacted> <redacted> <redacted>", result.description());
        }
    }
    static WireCore.Response errorResponse(String message) {
        var response = AdversarialTest.response("application/json", WireCore.dump(Map.of("error", Map.of("message", message)))); response.status = 401; return response;
    }
    @Test void carriageReturnOnlySseKeepsOutputAndFinalUsage() {
        String sse = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"}}]}\r\rdata: {\"choices\":[],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3}}\r\rdata: [DONE]\r\r";
        var result = AdversarialTest.build(AdversarialTest.chat("api.openai.com"), AdversarialTest.response("text/event-stream", sse));
        assertEquals("hello", result.attributes().get("output.value")); assertEquals(5L, result.attributes().get("gen_ai.usage.total_tokens"));
    }
    @Test void applicationProcessorDoesNotInvokeUnrequestedEndCallbacks() {
        var ends = new java.util.concurrent.atomic.AtomicInteger();
        var original = new io.opentelemetry.sdk.trace.SpanProcessor() {
            public void onStart(io.opentelemetry.context.Context context, io.opentelemetry.sdk.trace.ReadWriteSpan span) { }
            public boolean isStartRequired() { return false; }
            public void onEnd(io.opentelemetry.sdk.trace.ReadableSpan span) { ends.incrementAndGet(); }
            public boolean isEndRequired() { return false; }
        };
        var forwarding = (io.opentelemetry.sdk.trace.SpanProcessor)ApplicationOtel.processor(original);
        assertTrue(forwarding.isEndRequired()); forwarding.onEnd(null); assertEquals(0, ends.get());
        assertSame(forwarding, ApplicationOtel.processor(original), "lookup must not accumulate processors");
    }
    @Test void bedrockInvokeAcceptsEveryJsonTypeButNotInvalidBodies() {
        for (String json : List.of("[1,2]", "\"native input\"", "42", "true", "null", "{}")) {
            var request = new WireCore.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com/model/native%2Bmodel/invoke");
            request.headers.put("content-type", "application/json"); request.body = json.getBytes(StandardCharsets.UTF_8);
            var result = AdversarialTest.build(request, AdversarialTest.response("application/json", "{}"));
            assertNotNull(result, json); assertEquals(json, result.attributes().get("input.value")); assertEquals("native+model", result.attributes().get("gen_ai.request.model"));
        }
        var request = new WireCore.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke"); request.body = "{".getBytes(StandardCharsets.UTF_8);
        assertNull(AdversarialTest.build(request, AdversarialTest.response("application/json", "{}")));
    }
}
