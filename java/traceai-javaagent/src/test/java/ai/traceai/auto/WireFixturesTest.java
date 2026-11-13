package ai.traceai.auto;

import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.stream.Stream;
import static org.junit.jupiter.api.Assertions.*;

class WireFixturesTest {
    static final Path SPEC = Path.of("..", "..", "spec", "auto-instrumentation");
    @Test void catalogMatchesSpecByteForByte() throws Exception {
        try (var in = WireCore.class.getResourceAsStream("wire-protocols.json")) {
            assertArrayEquals(Files.readAllBytes(SPEC.resolve("wire-protocols.json")), Objects.requireNonNull(in).readAllBytes());
        }
    }
    @TestFactory Stream<DynamicTest> allSharedFixtures() throws Exception {
        List<Path> fixtures;
        try (var paths = Files.walk(SPEC.resolve("fixtures"))) { fixtures = paths.filter(p -> p.toString().endsWith(".json")).sorted().toList(); }
        assertTrue(fixtures.size() >= 47);
        return fixtures.stream().map(path -> DynamicTest.dynamicTest(path.getFileName().toString(), () -> runFixture(path)));
    }
    static byte[] bytes(Object o) { return WireCore.dump(o).getBytes(StandardCharsets.UTF_8); }
    static byte[] text(Object o) { return String.valueOf(o).getBytes(StandardCharsets.UTF_8); }
    static Map<String, String> headers(Object raw) {
        Map<String, String> headers = new LinkedHashMap<>(); WireCore.map(raw).forEach((k, v) -> headers.put(k, String.valueOf(v))); return headers;
    }
    static void add(BodyCapture capture, byte[] bytes) {
        for (int offset = 0; offset < bytes.length; offset += 8192) capture.add(ByteBuffer.wrap(bytes, offset, Math.min(8192, bytes.length - offset)));
    }
    static void zeros(BodyCapture capture, long size) {
        byte[] zeros = new byte[8192];
        while (size > 0) { int count = (int)Math.min(zeros.length, size); capture.add(ByteBuffer.wrap(zeros, 0, count)); size -= count; }
    }
    static WireCore.Request request(Map<String, Object> fixture) {
        WireCore.Request request = new WireCore.Request(WireCore.str(fixture.get("method")), WireCore.str(fixture.get("url")));
        request.headers = headers(fixture.get("headers"));
        if (Boolean.TRUE.equals(fixture.get("unavailable"))) { request.unavailable = true; return request; }
        if (fixture.containsKey("json")) request.headers.putIfAbsent("content-type", "application/json");
        if (fixture.containsKey("binary")) request.headers.putIfAbsent("content-type", WireCore.str(WireCore.get(fixture.get("binary"), "content_type")));
        String boundary = "traceaifixtureboundary";
        if (fixture.containsKey("multipart")) request.headers.put("content-type", "multipart/form-data; boundary=" + boundary);
        BodyCapture capture = new BodyCapture(WireCore.rawHeader(request.headers, "content-type"), 4194304, false);
        if (fixture.containsKey("json")) add(capture, bytes(fixture.get("json")));
        else if (fixture.containsKey("text")) add(capture, text(fixture.get("text")));
        else if (fixture.containsKey("binary")) zeros(capture, ((Number)WireCore.get(fixture.get("binary"), "size")).longValue());
        else if (fixture.containsKey("multipart")) {
            WireCore.map(WireCore.get(fixture.get("multipart"), "fields")).forEach((key, value) -> add(capture, text("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + key + "\"\r\n\r\n" + value + "\r\n")));
            for (Object file : WireCore.list(WireCore.get(fixture.get("multipart"), "files"))) {
                add(capture, text("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + WireCore.get(file, "name") + "\"; filename=\"" + WireCore.get(file, "filename") + "\"\r\nContent-Type: " + WireCore.get(file, "content_type") + "\r\n\r\n"));
                zeros(capture, ((Number)WireCore.get(file, "size")).longValue()); add(capture, text("\r\n"));
            }
            add(capture, text("--" + boundary + "--\r\n"));
        } else return request;
        request.body = capture.body(); request.size = capture.size; request.truncated = capture.truncated;
        if (capture.multipart != null) { request.fields = capture.multipart.fields; request.files = capture.multipart.files; request.fileBytes = capture.multipart.fileBytes; }
        return request;
    }
    static List<byte[]> split(byte[] bytes) { int middle = bytes.length / 2; return List.of(Arrays.copyOfRange(bytes, 0, middle), Arrays.copyOfRange(bytes, middle, bytes.length)); }
    static WireCore.Response response(Map<String, Object> fixture) throws Exception {
        WireCore.Response response = new WireCore.Response(); response.status = ((Number)fixture.get("status")).intValue(); response.headers = headers(fixture.get("headers"));
        List<byte[]> chunks = new ArrayList<>();
        if (fixture.containsKey("json")) chunks.add(bytes(fixture.get("json")));
        else if (fixture.containsKey("text")) chunks.addAll(split(text(fixture.get("text"))));
        else if (fixture.containsKey("sse")) for (Object item : WireCore.list(fixture.get("sse"))) {
            if (item instanceof String) chunks.add(text("data: " + item + "\n\n"));
            else {
                Map<String, Object> payload = new LinkedHashMap<>(WireCore.map(item)); Object event = payload.remove("event");
                chunks.add(text((event == null ? "" : "event: " + event + "\n") + "data: " + WireCore.dump(payload) + "\n\n"));
            }
        }
        else if (fixture.containsKey("ndjson")) for (Object item : WireCore.list(fixture.get("ndjson"))) chunks.add(text(WireCore.dump(item) + "\n"));
        else if (fixture.containsKey("binary")) chunks.addAll(split(new byte[((Number)WireCore.get(fixture.get("binary"), "size")).intValue()]));
        if (fixture.containsKey("chunk_sizes")) {
            ByteArrayOutputStream combined = new ByteArrayOutputStream();
            for (byte[] chunk : chunks) combined.write(chunk);
            byte[] wire = combined.toByteArray(); chunks = new ArrayList<>(); int offset = 0;
            for (Object length : WireCore.list(fixture.get("chunk_sizes"))) {
                int size = ((Number)length).intValue(); assertTrue(size >= 0 && size <= wire.length - offset);
                chunks.add(Arrays.copyOfRange(wire, offset, offset + size)); offset += size;
            }
            assertEquals(wire.length, offset, "fixture transport partition");
        }
        if (Boolean.TRUE.equals(fixture.get("gzip"))) {
            ByteArrayOutputStream encoded = new ByteArrayOutputStream();
            try (var gzip = new java.util.zip.GZIPOutputStream(encoded)) { for (byte[] chunk : chunks) gzip.write(chunk); }
            chunks = new ArrayList<>(split(encoded.toByteArray())); response.headers.put("content-encoding", "gzip");
        }
        if (fixture.get("error_after_chunks") instanceof Number count) {
            chunks = chunks.subList(0, Math.min(Math.max(0, count.intValue()), chunks.size())); response.error = new java.net.SocketTimeoutException("fixture timeout");
        }
        BodyCapture capture = new BodyCapture(WireCore.rawHeader(response.headers, "content-encoding").isEmpty() ? WireCore.rawHeader(response.headers, "content-type") : "application/json", 4194304, false);
        response.arrivals = new StreamFrames.Timeline();
        List<Object> offsets = WireCore.list(fixture.get("arrival_offsets_ms"));
        for (int i = 0; i < chunks.size(); i++) {
            byte[] chunk = chunks.get(i); double seconds = offsets.isEmpty() ? .001 : ((Number)offsets.get(i)).doubleValue() / 1000;
            add(capture, chunk);
            if (chunk.length > 0) {
                response.arrivals.add(capture.size, seconds);
                if (response.firstChunkSeconds == null) response.firstChunkSeconds = seconds;
            }
        }
        response.body = capture.body(); response.size = capture.size; response.truncated = capture.truncated;
        return response;
    }
    static void runFixture(Path path) throws Exception {
        Map<String, Object> fixture = WireCore.map(WireCore.parse(Files.readString(path))), expect = WireCore.map(fixture.get("expect"));
        if (String.valueOf(fixture.get("name")).startsWith("milestone-a10-bedrock-")) {
            // Spec 157-166 are decoded-event recognition cases, not binary HTTP conformance.
            WireCore.Response replay = response(WireCore.map(fixture.get("response")));
            var frames = StreamFrames.decode(replay.body, "text/event-stream").frames();
            Double first = null;
            for (var frame : frames) if (OutputMilestone.qualifies("bedrock.converse", frame.value())) { first = replay.arrivals.at(frame.end()); break; }
            var wanted = WireCore.map(expect.get("attributes"));
            assertEquals(wanted.get("gen_ai.response.time_to_first_chunk"), replay.firstChunkSeconds, path.toString());
            if (wanted.containsKey("gen_ai.response.time_to_first_text")) assertEquals(wanted.get("gen_ai.response.time_to_first_text"), first, path.toString());
            else assertNull(first, path.toString());
            return;
        }
        WireCore.Request request = request(WireCore.map(fixture.get("request")));
        WireCore.Match match = WireCore.match(request.method, request.url);
        WireCore.Result result = match == null ? null : WireCore.build(match, request, response(WireCore.map(fixture.get("response"))));
        if (Boolean.FALSE.equals(expect.get("captured"))) { assertNull(result); return; }
        assertNotNull(result); assertEquals(expect.get("name"), result.name()); assertEquals("OK".equals(expect.get("status")), result.ok());
        if (expect.containsKey("status_description")) assertEquals(expect.get("status_description"), result.description());
        WireCore.map(expect.get("attributes")).forEach((key, want) -> {
            assertTrue(result.attributes().containsKey(key), "missing " + key);
            assertEquals(WireCore.JSON.toJsonTree(want), WireCore.JSON.toJsonTree(result.attributes().get(key)), key);
        });
        for (Object key : WireCore.list(expect.get("present"))) assertTrue(result.attributes().containsKey(key), "missing " + key);
        for (Object key : WireCore.list(expect.get("absent"))) assertFalse(result.attributes().containsKey(key), "unexpected " + key);
        WireCore.map(expect.get("not_contains")).forEach((key, needles) -> {
            for (Object needle : WireCore.list(needles)) assertFalse(String.valueOf(result.attributes().get(key)).contains(String.valueOf(needle)), key + " contains forbidden substring");
        });
    }
}
