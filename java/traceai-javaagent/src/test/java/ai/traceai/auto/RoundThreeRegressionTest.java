package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class RoundThreeRegressionTest {
    @Test void truncatedAndMalformedJsonFailClosedAtEveryExportSurface() throws Exception {
        String privateJson = "{\"password\":\"WRAPPER_PRIVATE_PASSWORD\",\"prompt\":\"" + "x".repeat(40000) + "\"}";
        String truncated = privateJson.substring(0, 32000) + "…";
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("test").spanBuilder("library").startSpan();
            for (String key : List.of("fi.raw_input", "input.value", "output.value", "gen_ai.request.parameters")) span.setAttribute(key, truncated);
            span.setStatus(StatusCode.ERROR, truncated);
            span.addEvent("exception", Attributes.of(AttributeKey.stringKey("exception.message"), truncated));
            span.end(); var original = memory.getFinishedSpanItems().get(0);
            for (String hide : List.of("hide_inputs", "hide_outputs")) {
                var config = AutoConfig.resolve(Map.of(hide, true), Map.of());
                for (var data : List.of(original, ApplicationOtel.snapshot(original, getClass().getClassLoader(), config, TraceAIAuto.resource(config)))) {
                    assertFalse(WireCore.dump(new OtlpJsonExporter(config).serialize(data)).contains("WRAPPER_PRIVATE_PASSWORD"));
                }
            }
            assertEquals(truncated, original.getAttributes().get(AttributeKey.stringKey("fi.raw_input")));
            assertEquals("<redacted>", new WirePrivacy(Map.of(), null).text(truncated));
            assertEquals("<redacted>", new WirePrivacy(Map.of(), null).text("[{\"private_key\":\"another-secret"));
        }
    }
    @Test void nonFiniteScalarAndArrayAttributesDoNotDropMixedExportBatch() throws Exception {
        var payload = new AtomicReference<String>();
        var server = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> {
            payload.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            exchange.sendResponseHeaders(200, -1); exchange.close();
        }); server.start();
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            provider.get("test").spanBuilder("ordinary").startSpan().end();
            var span = provider.get("test").spanBuilder("non-finite").startSpan();
            span.setAttribute("nan", Double.NaN); span.setAttribute("positive", Double.POSITIVE_INFINITY); span.setAttribute("negative", Double.NEGATIVE_INFINITY);
            span.setAttribute(AttributeKey.doubleArrayKey("array"), List.of(1d, Double.NaN, Double.POSITIVE_INFINITY, Double.NEGATIVE_INFINITY));
            span.addEvent("non-finite", Attributes.of(AttributeKey.doubleKey("event.nan"), Double.NaN)); span.end();
            var config = AutoConfig.resolve(Map.of("api_key", "test-api", "secret_key", "test-secret", "base_url", "http://127.0.0.1:" + server.getAddress().getPort()), Map.of());
            assertTrue(new OtlpJsonExporter(config).export(memory.getFinishedSpanItems()).isSuccess());
            assertEquals(2, WireCore.list(WireCore.get(WireCore.parse(payload.get()), "resourceSpans")).size());
            for (String value : List.of("NaN", "Infinity", "-Infinity")) assertTrue(payload.get().contains("\"doubleValue\":\"" + value + "\""));
            assertTrue(Double.isNaN(memory.getFinishedSpanItems().get(1).getAttributes().get(AttributeKey.doubleKey("nan"))));
        } finally { server.stop(0); }
    }
    @Test void falseClosingMultipartDelimitersRetainEveryFileByteAndFollowingModel() {
        for (String falseTail : List.of("--X", "--\rX", "--\r\r\n", "-- \tX")) {
            String file = "audio\r\n--b" + falseTail + "PRIVATE_FILE_PAYLOAD";
            String body = "--b\r\nContent-Disposition: form-data; name=file; filename=a.wav\r\nContent-Type: audio/wav\r\n\r\n" + file + "\r\n--b\r\nContent-Disposition: form-data; name=model\r\n\r\nwhisper-1\r\n--b--\r\n";
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            for (int split = 0; split <= bytes.length; split++) {
                var capture = new BodyCapture("multipart/form-data; boundary=b", 16, false);
                capture.add(ByteBuffer.wrap(bytes, 0, split)); capture.add(ByteBuffer.wrap(bytes, split, bytes.length - split)); capture.body();
                assertEquals(file.length(), capture.multipart.fileBytes); assertEquals(file.length(), capture.multipart.files.get(0).size());
                assertEquals(Map.of("model", "whisper-1"), capture.multipart.fields);
                var request = new WireCore.Request("POST", "https://api.openai.com/v1/audio/transcriptions");
                request.fields = capture.multipart.fields; request.files = capture.multipart.files; request.fileBytes = capture.multipart.fileBytes;
                var result = AdversarialTest.build(request, AdversarialTest.response("application/json", "{\"text\":\"ok\"}"));
                assertNotNull(result); assertEquals((long)file.length(), result.attributes().get("traceai.audio.input_bytes"));
                assertFalse(WireCore.dump(result.attributes()).contains("PRIVATE_FILE_PAYLOAD"));
            }
        }
    }
    @Test void closingMultipartDelimiterAcceptsPaddingAndEof() {
        for (String tail : List.of("--", "-- \t", "--\r\n", "-- \t\r\n")) {
            var capture = new BodyCapture("multipart/form-data; boundary=b", 32, false);
            capture.add(ByteBuffer.wrap(("--b\r\nContent-Disposition: form-data; name=model\r\n\r\nm\r\n--b" + tail).getBytes(StandardCharsets.UTF_8)));
            capture.body(); assertEquals(Map.of("model", "m"), capture.multipart.fields);
        }
    }
    @Test void unavailablePollingBodiesCannotBypassTerminalStatusFilter() {
        for (String body : List.of("", "not-json", "[]", "null", "{\"status\":\"processing\"}", "{}")) {
            var request = new WireCore.Request("GET", "https://api.assemblyai.com/v2/transcript/id");
            assertNull(AdversarialTest.build(request, AdversarialTest.response("application/json", body)), body);
        }
        for (String status : List.of("completed", "error")) {
            assertNotNull(AdversarialTest.build(new WireCore.Request("GET", "https://api.assemblyai.com/v2/transcript/id"), AdversarialTest.response("application/json", "{\"status\":\"" + status + "\"}")));
        }
        var request = new WireCore.Request("GET", "https://api.assemblyai.com/v2/transcript/id");
        var response = AdversarialTest.response("application/json", ""); response.error = new java.io.IOException("transport reset");
        assertFalse(AdversarialTest.build(request, response).ok());
    }
    @Test void singleRecordNdjsonAndJsonlAreStreamsWithUsageAndTiming() {
        for (String type : List.of("application/x-ndjson", "application/ndjson", "application/jsonl")) {
            var request = AdversarialTest.request("http://localhost:11434/api/chat", Map.of("model", "m", "messages", List.of()));
            var response = AdversarialTest.response(type, "{\"message\":{\"content\":\"hi\"},\"done\":true,\"done_reason\":\"stop\",\"prompt_eval_count\":2,\"eval_count\":3}\n");
            response.firstChunkSeconds = .25;
            var result = AdversarialTest.build(request, response);
            assertEquals("hi", result.attributes().get("output.value")); assertEquals(true, result.attributes().get("gen_ai.request.stream"));
            assertEquals(.25, result.attributes().get("gen_ai.response.time_to_first_chunk")); assertEquals(5L, result.attributes().get("gen_ai.usage.total_tokens"));
        }
    }
    @Test void ollamaToolOnlyAndMixedOutputsPreserveCallsAndArguments() {
        for (String type : List.of("application/json", "application/x-ndjson")) for (String content : List.of("", "checking")) {
            var request = AdversarialTest.request("http://localhost:11434/api/chat", Map.of("messages", List.of()));
            var response = AdversarialTest.response(type, WireCore.dump(Map.of("message", Map.of("role", "assistant", "content", content, "tool_calls", List.of(Map.of("function", Map.of("name", "weather", "arguments", Map.of("city", "Paris"))))))) + "\n");
            var result = AdversarialTest.build(request, response);
            assertEquals("weather", result.attributes().get("gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"));
            assertEquals("{\"city\":\"Paris\"}", result.attributes().get("gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"));
            assertNotNull(result.attributes().get("output.value"));
        }
    }
    @Test void bridgePreservesParentTracestateFlagsAndRemoteBit() {
        for (boolean remote : List.of(false, true)) {
            var state = TraceState.builder().put("second", "two").put("first", "one").build();
            var original = remote ? SpanContext.createFromRemoteParent("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), state) : SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getDefault(), state);
            try (var ignored = Span.wrap(original).makeCurrent()) {
                var parent = Span.fromContext(OtelBridge.snapshot().parent()).getSpanContext();
                assertEquals(original, parent); assertEquals(state.asMap(), parent.getTraceState().asMap()); assertEquals(new ArrayList<>(state.asMap().keySet()), new ArrayList<>(parent.getTraceState().asMap().keySet()));
            }
        }
    }
    @Test void anthropicStreamJoinsBlocksWithNewlineButNotTheirDeltas() {
        String stream = "";
        for (int i = 0; i < 2; i++) {
            stream += "data: " + WireCore.dump(Map.of("type", "content_block_start", "index", i, "content_block", Map.of("type", "text", "text", "a"))) + "\n\n";
            stream += "data: " + WireCore.dump(Map.of("type", "content_block_delta", "index", i, "delta", Map.of("type", "text_delta", "text", "b"))) + "\n\n";
        }
        var request = AdversarialTest.request("https://api.anthropic.com/v1/messages", Map.of("messages", List.of()));
        assertEquals("ab\nab", AdversarialTest.build(request, AdversarialTest.response("text/event-stream", stream)).attributes().get("output.value"));
    }
}
