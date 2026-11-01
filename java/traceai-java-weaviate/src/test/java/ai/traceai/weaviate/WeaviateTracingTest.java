package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;

import static org.assertj.core.api.Assertions.*;

class WeaviateTracingTest {
    @Test void preservesCheckedAndUncheckedExceptionIdentity() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracing = new WeaviateTracing("Article", new FITracer(provider.get("test"), TraceConfig.builder().hideErrorDetails(true).build()));
            var checked = new IOException("private");
            var unchecked = new IllegalArgumentException("private");
            var error = new AssertionError("private");
            assertThatThrownBy(() -> tracing.callIO("insert", () -> { throw checked; })).isSameAs(checked);
            assertThatThrownBy(() -> tracing.call("query", FISpanKind.RETRIEVER, null, () -> { throw unchecked; })).isSameAs(unchecked);
            assertThatThrownBy(() -> tracing.call("query", FISpanKind.RETRIEVER, null, () -> { throw error; })).isSameAs(error);
            assertThat(exporter.getFinishedSpanItems()).hasSize(3).allSatisfy(s -> {
                assertThat(s.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
                assertThat(s.getAttributes().toString() + s.getEvents()).doesNotContain("private");
            });
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
        }
    }

    @Test void returnsOriginalFutureAndOriginalResultAndEndsExactlyOnce() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracing = new WeaviateTracing("Article", new FITracer(provider.get("test"), TraceConfig.builder().build()));
            var pending = new CompletableFuture<Object>();
            var result = new Object();
            assertThat(tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> pending)).isSameAs(pending);
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            assertThat(pending.complete(result)).isTrue();
            assertThat(pending.join()).isSameAs(result);
            assertThat(pending.complete(new Object())).isFalse();
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(tracing.call("query", FISpanKind.RETRIEVER, null, () -> result)).isSameAs(result);
            var cancelled = new CompletableFuture<Object>();
            var original = tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> cancelled);
            original.cancel(true);
            assertThat(cancelled).isCancelled();
            assertThat(exporter.getFinishedSpanItems()).hasSize(3);
        }
    }

    @Test void telemetryFailureCannotPreventTheOperationOrReplaceItsException() {
        var tracer = new FITracer(io.opentelemetry.api.OpenTelemetry.noop().getTracer("test"), TraceConfig.builder().build()) {
            @Override public Span startSpan(String name, FISpanKind kind) {
                throw new IllegalStateException("broken telemetry backend");
            }
            @Override public void setError(Span span, Throwable error) {
                throw new IllegalStateException("broken error recording");
            }
        };
        var tracing = new WeaviateTracing("Article", tracer);
        var result = new Object();
        try (var provider = SdkTracerProvider.builder().build()) {
            var parent = provider.get("test").spanBuilder("parent").startSpan();
            try (var ignored = parent.makeCurrent()) {
                assertThat(tracing.call("query", FISpanKind.RETRIEVER, null, () -> {
                    assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
                    return result;
                })).isSameAs(result);
                assertThat(Span.current()).isSameAs(parent);
            } finally {
                parent.end();
            }
        }
        var failure = new IOException("original SDK failure");
        assertThatThrownBy(() -> tracing.callIO("insert", () -> { throw failure; })).isSameAs(failure);
        var future = CompletableFuture.completedFuture(result);
        assertThat(tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> future)).isSameAs(future);
    }

    @Test void handlesAlreadyCompletedAndSynchronouslyFailedAsyncCalls() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracing = new WeaviateTracing("Article", new FITracer(provider.get("test"), TraceConfig.builder().build()));
            var done = CompletableFuture.completedFuture("result");
            assertThat(tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> done)).isSameAs(done);
            var failure = new IllegalArgumentException("failure");
            var failed = CompletableFuture.failedFuture(failure);
            assertThat(tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> failed)).isSameAs(failed);
            assertThatThrownBy(failed::join).isInstanceOf(CompletionException.class).hasCause(failure);
            assertThatThrownBy(() -> tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> { throw failure; })).isSameAs(failure);
            assertThat(exporter.getFinishedSpanItems()).hasSize(3);
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
        }
    }
}
