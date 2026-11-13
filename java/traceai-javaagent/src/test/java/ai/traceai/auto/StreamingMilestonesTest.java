package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class StreamingMilestonesTest {
    static final long EPOCH = 1700000000000000000L;
    private SpanData transport(String url, String requestBody, String[] chunks, int[] ms) {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var config = AutoConfig.resolve(Map.of(), Map.of());
            var runtime = new TraceAIAuto.RuntimeState(config, provider, provider.get("milestones"), new OtlpJsonExporter(config));
            var request = new WireCore.Request("POST", url); long start = System.nanoTime();
            var context = new OtelBridge.Snapshot(Context.root(), Map.of(), false, EPOCH, start);
            var capture = new WireCapture(request, Map.of("content-type", "application/json"), WireCore.match(request.method, request.url), context, 4194304, runtime);
            capture.requestBytes(ByteBuffer.wrap(requestBody.getBytes(StandardCharsets.UTF_8))); capture.requestComplete();
            capture.responseHeaders(200, Map.of("content-type", "text/event-stream"));
            for (int i = 0; i < chunks.length; i++) {
                ByteBuffer source = ByteBuffer.wrap(chunks[i].getBytes(StandardCharsets.UTF_8));
                capture.responseBytes(source, start + ms[i] * 1000000L); assertEquals(0, source.position());
            }
            capture.finish(null); return memory.getFinishedSpanItems().get(0);
        }
    }
    @Test void transportArrivalNotParsingOrSpanEndSetsFirstOutput() {
        String[] chunks = {"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n", "data: {\"choices\":[{\"delta\":{\"refusal\":\"No\"}}]}\n", "\n", "data: {\"choices\":[{\"delta\":{\"content\":\"Later\"}}]}\n\n"};
        var span = transport("http://127.0.0.1/v1/chat/completions", "{\"messages\":[],\"stream\":true}", chunks, new int[]{10, 20, 30, 70});
        assertEquals(.01, span.getAttributes().get(AttributeKey.doubleKey("gen_ai.response.time_to_first_chunk")));
        assertEquals(.03, span.getAttributes().get(AttributeKey.doubleKey("gen_ai.response.time_to_first_text")));
        assertEquals(EPOCH + 70000000L, span.getEndEpochNanos());
    }
    @Test void strippedAudioStillQualifiesAtItsCompleteEventArrival() {
        String[] chunks = {"data: {\"type\":\"chunk\",\"data\":\"\"}\n\n", "data: {\"type\":\"chunk\",\"data\":\"UklGRgAAAABXQVZF\"}\n", "\n", "data: {\"type\":\"done\"}\n\n"};
        var span = transport("https://api.cartesia.ai/tts/sse", "{\"transcript\":\"Hi\"}", chunks, new int[]{10, 20, 30, 70});
        assertEquals(.03, span.getAttributes().get(AttributeKey.doubleKey("gen_ai.response.time_to_first_text")));
        assertEquals(12L, span.getAttributes().get(AttributeKey.longKey("traceai.audio.output_bytes")));
        assertFalse(WireCore.dump(new OtlpJsonExporter(AutoConfig.resolve(Map.of(), Map.of())).serialize(span)).contains("UklGRgAAAABXQVZF"));
    }
    @Test void boundedArrivalLossOmitsUnknownOutputTimeButKeepsEarlierKnownTime() {
        String event = "data: {\"choices\":[{\"delta\":{\"content\":\"output\"}}]}\n\n";
        for (boolean early : List.of(false, true)) {
            var request = AdversarialTest.request("http://127.0.0.1/v1/chat/completions", Map.of("messages", List.of()));
            var response = AdversarialTest.response("text/event-stream", early ? event + "\n".repeat(9000) : "\n".repeat(9000) + event);
            response.arrivals = new StreamFrames.Timeline(); response.firstChunkSeconds = .001;
            for (int i = 1; i <= 9000; i++) response.arrivals.add(i, .001);
            assertTrue(response.arrivals.lost);
            var result = AdversarialTest.build(request, response);
            assertEquals(true, result.attributes().get("traceai.wire.incomplete"));
            assertEquals(early ? .001 : null, result.attributes().get("gen_ai.response.time_to_first_text"));
            assertEquals(.001, result.attributes().get("gen_ai.response.time_to_first_chunk"));
        }
    }
}
