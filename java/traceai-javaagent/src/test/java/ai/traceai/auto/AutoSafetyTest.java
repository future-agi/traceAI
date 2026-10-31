package ai.traceai.auto;

import org.junit.jupiter.api.Test;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class AutoSafetyTest {
    @Test void configurationPrecedenceAndMaskedSecrets() throws Exception {
        Path file = Files.createTempFile(Path.of("target"), "config-", ".toml");
        Files.writeString(file, "[ignored]\nproject_name = \"file#project\" # comment\nsample_rate = 0.25\ninstrumentations = [\"wire\", \"openai\"]\ndebug = true\nwire_capture = false\n");
        AutoConfig config = AutoConfig.resolve(Map.of("project_name", "code-project"), Map.of("FI_CONFIG_FILE", file.toString(), "FI_PROJECT_NAME", "env-project", "FI_API_KEY", "secret", "FI_SECRET_KEY", "also-secret"));
        assertEquals("code-project", config.get("FI_PROJECT_NAME")); assertEquals(.25, config.sampleRate); assertEquals("observe", config.get("FI_PROJECT_TYPE")); assertTrue(config.flag("FI_AUTO_DEBUG")); assertFalse(config.flag("FI_WIRE_CAPTURE")); assertTrue(config.enabled("wire")); assertFalse(config.enabled("bedrock")); assertFalse(config.masked().toString().contains("also-secret"));
        assertEquals("file#project", AutoConfig.resolve(Map.of("config_file", file.toString()), Map.of()).get("FI_PROJECT_NAME"));
        assertFalse(AutoConfig.resolve(Map.of("instrumentations", List.of("wire"), "disabled_instrumentations", List.of("wire")), Map.of()).enabled("wire"));
    }
    @Test void malformedConfigurationDefaultsFailOpen() {
        AutoConfig config = AutoConfig.resolve(Map.of("config_file", "target/no-such-file", "sample_rate", "NaN", "wire_max_body_bytes", "invalid"), Map.of());
        assertEquals(1, config.sampleRate); assertEquals(4194304, config.maxBodyBytes);
    }
    @Test void boundedCaptureDoesNotMoveApplicationBuffer() {
        ByteBuffer buffer = ByteBuffer.wrap("0123456789".getBytes(StandardCharsets.UTF_8)); buffer.position(2);
        BodyCapture body = new BodyCapture("application/json", 3, false); body.add(buffer);
        assertEquals(2, buffer.position()); assertEquals(10, buffer.limit()); assertEquals(8, body.size); assertTrue(body.truncated); assertEquals(0, body.bytes.size());
    }
    @Test void binaryIsOnlyCounted() {
        BodyCapture body = new BodyCapture("audio/mpeg", 1, false);
        for (int i = 0; i < 100; i++) body.add(ByteBuffer.wrap(new byte[1024]));
        assertEquals(102400, body.size); assertNull(body.body()); assertEquals(0, body.bytes.size()); assertFalse(body.truncated);
    }
    @Test void multipartFilesAreNotBufferedAcrossChunks() {
        String boundary = "testBoundary";
        BodyCapture body = new BodyCapture("multipart/form-data; boundary=" + boundary, 1024, false);
        String head = "--" + boundary + "\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
        byte[] prefix = head.getBytes(StandardCharsets.UTF_8); for (byte b : prefix) body.add(ByteBuffer.wrap(new byte[]{b}));
        for (int i = 0; i < 100; i++) body.add(ByteBuffer.wrap(new byte[1024]));
        for (byte b : ("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8)) body.add(ByteBuffer.wrap(new byte[]{b}));
        assertEquals("whisper-1", body.multipart.fields.get("model")); assertEquals(1, body.multipart.files.size()); assertEquals(102400, body.multipart.files.get(0).size()); assertEquals(0, body.bytes.size()); assertFalse(body.truncated);
    }
    @Test void encodedAudioIsCountedNotRetained() {
        for (String type : List.of("application/json", "text/event-stream")) {
            BodyCapture body = new BodyCapture(type, 1, true);
            String encoded = "data: {\"audio_base64\":\"AQIDBA==\",\"data\":\"AQID\"}\n\n";
            for (byte b : encoded.getBytes(StandardCharsets.UTF_8)) body.add(ByteBuffer.wrap(new byte[]{b}));
            assertEquals(7, body.audio.total); assertFalse(new String(body.body(), StandardCharsets.UTF_8).contains("AQID")); assertEquals(0, body.bytes.size());
        }
    }
    @Test void suppressionAndPrivacyHelpers() throws Exception {
        try (var ignored = TraceAIAuto.suppress()) { assertTrue(OtelBridge.snapshot().suppressed()); }
        TraceAIAuto.IN_FLIGHT.set(true);
        try { assertTrue(OtelBridge.snapshot().suppressed()); } finally { TraceAIAuto.IN_FLIGHT.remove(); }
        Object clean = TraceAIAuto.scrub(Map.of("api_key", "provider-key", "nested", Map.of("authorization", "bearer"), "input_audio", Map.of("data", "AQID".repeat(100), "format", "wav")));
        assertFalse(WireCore.dump(clean).contains("provider-key")); assertFalse(WireCore.dump(clean).contains("bearer")); assertFalse(WireCore.dump(clean).contains("AQID"));
    }
    @Test void allCatalogFormatsHaveExecutableParserPaths() {
        // Exercise the formats not represented by the 32 original golden cases.
        Map<String, String[]> cases = new LinkedHashMap<>();
        cases.put("https://api.openai.com/v1/completions", new String[]{"{\"model\":\"m\",\"prompt\":\"Hi\"}", "{\"choices\":[{\"text\":\"reply\"}]}"});
        cases.put("https://api.openai.com/v1/images/generations", new String[]{"{\"prompt\":\"Hi\"}", "{\"data\":[{\"b64_json\":\"AQID\"}]}"});
        cases.put("https://api.openai.com/v1/moderations", new String[]{"{\"input\":\"Hi\"}", "{\"results\":[{\"flagged\":true}]}"});
        cases.put("https://generativelanguage.googleapis.com/v1/models/m:embedContent", new String[]{"{\"content\":{}}", "{\"embedding\":{\"values\":[1,2]}}"});
        cases.put("https://api.cohere.com/v2/embed", new String[]{"{\"texts\":[\"hi\"]}", "{\"embeddings\":{\"float\":[[1,2]]}}"});
        cases.put("https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke", new String[]{"{\"prompt\":\"hi\"}", "{\"completion\":\"reply\"}"});
        cases.put("http://localhost:11434/api/generate", new String[]{"{\"model\":\"m\",\"prompt\":\"hi\"}", "{\"response\":\"reply\",\"done\":true}"});
        cases.put("http://localhost:11434/api/embed", new String[]{"{\"model\":\"m\",\"input\":[\"hi\"]}", "{\"embeddings\":[[1,2]]}"});
        cases.forEach((url, bodies) -> {
            WireCore.Request req = new WireCore.Request("POST", url); req.body = bodies[0].getBytes(StandardCharsets.UTF_8);
            WireCore.Response resp = new WireCore.Response(); resp.status = 200; resp.body = bodies[1].getBytes(StandardCharsets.UTF_8); resp.headers.put("content-type", "application/json");
            WireCore.Result result = WireCore.build(Objects.requireNonNull(WireCore.match("POST", url)), req, resp); assertNotNull(result, url);
            if (url.contains("images")) { assertTrue(String.valueOf(result.attributes().get("output.value")).contains("<base64 omitted>")); assertFalse(result.attributes().toString().contains("AQID")); }
            if (url.endsWith("moderations")) assertEquals(true, result.attributes().get("traceai.moderation.flagged"));
            if (url.endsWith("embed") || url.endsWith("embedContent")) assertEquals(2, result.attributes().get("gen_ai.embeddings.dimension.count"));
        });
    }
}
