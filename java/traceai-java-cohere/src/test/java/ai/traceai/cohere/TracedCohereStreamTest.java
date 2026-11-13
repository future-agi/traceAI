package ai.traceai.cohere;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.Closeable;
import java.io.IOException;
import java.util.Iterator;
import java.util.List;

import static ai.traceai.cohere.TracedCohereTransportTest.*;
import static org.assertj.core.api.Assertions.*;

class TracedCohereStreamTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @ValueSource(strings = {"iterator", "hasNext", "next"})
    void iteratorFailuresKeepOriginalExceptionAndCloseExactlyOnce(String stage) throws Exception {
        var source = new Source();
        source.failureStage = stage;
        var stream = stream(source);
        assertThatThrownBy(() -> stream.iterator().next()).isSameAs(source.failure);
        stream.close();
        assertThat(source.closes).isEqualTo(1);
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(otel.getSpans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void closeFailureDoesNotMaskIterationFailure() throws Exception {
        var source = new Source();
        source.failureStage = "next";
        source.closeFailure = new IOException("private-close-error");
        var stream = stream(source);
        assertThatThrownBy(() -> stream.iterator().next()).isSameAs(source.failure);
        assertThat(source.failure.getSuppressed()).containsExactly(source.closeFailure);
        stream.close();
        assertThat(source.closes).isEqualTo(1);
        assertThat(otel.getSpans()).hasSize(1);
    }

    @Test
    void explicitCloseFailureEndsSpanAndPropagates() {
        var source = new Source();
        source.closeFailure = new IOException("private-close-error");
        var stream = stream(source);
        assertThatThrownBy(stream::close).isSameAs(source.closeFailure);
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(otel.getSpans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void exhaustedIncompleteStreamClosesAndReturnsSameEvent() throws Exception {
        var source = new Source();
        var stream = stream(source);
        var iterator = stream.iterator();
        assertThat(iterator.next()).isSameAs(source.event);
        assertThat(iterator.hasNext()).isFalse();
        assertThat(iterator.hasNext()).isFalse();
        stream.close();
        assertThat(source.closes).isEqualTo(1);
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(value(otel.getSpans().get(0), "cohere.stream.outcome")).isEqualTo("incomplete");
        assertThatThrownBy(stream::iterator).isInstanceOf(IllegalStateException.class);
    }

    @Test
    void unopenedStreamCanBeClosedWithoutCreatingAnIterator() throws Exception {
        var source = new Source();
        source.failureStage = "iterator";
        var stream = stream(source);
        stream.close();
        assertThat(stream.iterator().hasNext()).isFalse();
        assertThat(source.closes).isEqualTo(1);
        assertThat(otel.getSpans()).hasSize(1);
    }

    @Test
    void streamedTextIsBounded() throws Exception {
        var transport = new FakeTransport(V2_STREAM.replace("private-output", "x".repeat(100000)));
        var traced = new TracedCohereClient(transport.client(), tracer());
        try (var stream = TracedCohereTransportTest.stream(traced, true)) { stream.forEach(event -> {}); }
        assertThat(value(otel.getSpans().get(0), SemanticConventions.OUTPUT_VALUE)).hasSize(32000);
        assertThat(otel.getSpans().get(0).getAttributes().get(
            io.opentelemetry.api.common.AttributeKey.booleanKey("cohere.stream.output_truncated"))).isTrue();
    }

    private FITracer tracer() {
        return new FITracer(otel.getOpenTelemetry().getTracer("stream-test"), TraceConfig.builder().hideErrorDetails(true).build());
    }

    private TracedCohereStream<Object> stream(Source source) {
        FITracer tracer = tracer();
        return new TracedCohereStream<>(source, tracer, tracer.startSpan("test stream", FISpanKind.LLM),
            event -> TracedCohereStream.EventState.CONTINUE, () -> {});
    }

    static final class Source implements Iterable<Object>, Closeable {
        final Object event = new Object();
        final RuntimeException failure = new IllegalStateException("private-error");
        String failureStage;
        IOException closeFailure;
        int closes;

        @Override
        public Iterator<Object> iterator() {
            if ("iterator".equals(failureStage)) throw failure;
            Iterator<Object> delegate = List.of(event).iterator();
            return new Iterator<>() {
                @Override public boolean hasNext() {
                    if ("hasNext".equals(failureStage)) throw failure;
                    return delegate.hasNext();
                }
                @Override public Object next() {
                    if ("next".equals(failureStage)) throw failure;
                    return delegate.next();
                }
            };
        }

        @Override public void close() throws IOException {
            closes++;
            if (closeFailure != null) throw closeFailure;
        }
    }
}
