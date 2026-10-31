package ai.traceai.privacy;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.testing.trace.TestSpanData;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.data.StatusData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.junit.jupiter.api.Test;

import java.util.Collection;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class ErrorDetailRedactingSpanExporterTest {
    private static final AttributeKey<String> MESSAGE = AttributeKey.stringKey("exception.message");
    private static final AttributeKey<String> STACK = AttributeKey.stringKey("exception.stacktrace");
    private static final AttributeKey<String> TYPE = AttributeKey.stringKey("exception.type");

    static final class RecordingExporter implements SpanExporter {
        List<SpanData> spans;
        final CompletableResultCode exported = CompletableResultCode.ofFailure();
        final CompletableResultCode flushed = CompletableResultCode.ofSuccess();
        final CompletableResultCode stopped = CompletableResultCode.ofSuccess();
        int exports;
        int flushes;
        int shutdowns;

        public CompletableResultCode export(Collection<SpanData> values) {
            spans = List.copyOf(values);
            exports++;
            return exported;
        }
        public CompletableResultCode flush() { flushes++; return flushed; }
        public CompletableResultCode shutdown() { shutdowns++; return stopped; }
    }

    private static TestSpanData.Builder span() {
        return TestSpanData.builder().setName("request").setKind(io.opentelemetry.api.trace.SpanKind.SERVER)
                .setHasEnded(true).setSpanContext(SpanContext.getInvalid())
                .setParentSpanContext(SpanContext.getInvalid()).setStartEpochNanos(10).setEndEpochNanos(20);
    }

    @Test
    void sanitizesEventsAndSpanAttributesWithoutMutatingTheSourceOrDroppingMetadata() {
        Attributes raw = Attributes.of(MESSAGE, "private message", STACK, "private stack", TYPE, "Failure");
        EventData event = EventData.create(15, "exception", raw, 5);
        SpanData original = span().setAttributes(raw).setTotalAttributeCount(6)
                .setStatus(StatusData.create(StatusCode.ERROR, "private status"))
                .setEvents(List.of(event)).setTotalRecordedEvents(3).build();
        RecordingExporter sink = new RecordingExporter();
        SpanExporter exporter = ErrorDetailRedactingSpanExporter.wrap(sink);
        assertSame(sink.exported, exporter.export(List.of(original)));
        SpanData filtered = sink.spans.get(0);
        assertEquals(Attributes.of(TYPE, "Failure"), filtered.getAttributes());
        assertEquals(4, filtered.getTotalAttributeCount());
        assertEquals(StatusCode.ERROR, filtered.getStatus().getStatusCode());
        assertEquals("", filtered.getStatus().getDescription());
        assertEquals(Attributes.of(TYPE, "Failure"), filtered.getEvents().get(0).getAttributes());
        assertEquals(3, filtered.getEvents().get(0).getTotalAttributeCount());
        assertEquals(15, filtered.getEvents().get(0).getEpochNanos());
        assertEquals("exception", filtered.getEvents().get(0).getName());
        assertEquals(original.getSpanContext(), filtered.getSpanContext());
        assertEquals(original.getResource(), filtered.getResource());
        assertEquals(original.getInstrumentationScopeInfo(), filtered.getInstrumentationScopeInfo());
        assertEquals(3, filtered.getTotalRecordedEvents());
        assertEquals(10, filtered.getStartEpochNanos());
        assertEquals(20, filtered.getEndEpochNanos());
        assertThrows(UnsupportedOperationException.class, () -> filtered.getEvents().clear());
        assertEquals(raw, original.getAttributes());
        assertEquals(raw, original.getEvents().get(0).getAttributes());
        assertEquals("private status", original.getStatus().getDescription());
    }

    @Test
    void safeSpansAndUnrelatedAttributesPassThrough() {
        RecordingExporter sink = new RecordingExporter();
        SpanData original = span().setAttributes(Attributes.of(AttributeKey.stringKey("input.value"), "application data"))
                .setEvents(List.of(EventData.create(15, "notice", Attributes.of(TYPE, "Failure"))))
                .setStatus(StatusData.unset()).build();
        ErrorDetailRedactingSpanExporter.wrap(sink).export(List.of(original));
        assertSame(original, sink.spans.get(0));
    }

    @Test
    void removesSensitiveKeysRegardlessOfAttributeTypeOrEventName() {
        RecordingExporter sink = new RecordingExporter();
        SpanData original = span().setEvents(List.of(EventData.create(15, "custom",
                Attributes.of(AttributeKey.longKey("exception.message"), 123L))))
                .setStatus(StatusData.unset()).build();
        ErrorDetailRedactingSpanExporter.wrap(sink).export(List.of(original));
        assertTrue(sink.spans.get(0).getEvents().get(0).getAttributes().isEmpty());
    }

    @Test
    void wrapIsIdempotentAndDelegatesLifecycleAndResultsExactlyOnce() {
        RecordingExporter sink = new RecordingExporter();
        SpanExporter exporter = ErrorDetailRedactingSpanExporter.wrap(sink);
        assertSame(exporter, ErrorDetailRedactingSpanExporter.wrap(exporter));
        assertSame(sink.exported, exporter.export(List.of()));
        assertSame(sink.flushed, exporter.flush());
        assertSame(sink.stopped, exporter.shutdown());
        assertEquals(1, sink.exports);
        assertEquals(1, sink.flushes);
        assertEquals(1, sink.shutdowns);
        assertTrue(sink.spans.isEmpty());
        assertThrows(NullPointerException.class, () -> ErrorDetailRedactingSpanExporter.wrap(null));
    }
}
