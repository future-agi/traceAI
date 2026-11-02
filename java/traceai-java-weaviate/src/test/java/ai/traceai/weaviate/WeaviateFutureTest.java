package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.*;

class WeaviateFutureTest {
    @ParameterizedTest
    @ValueSource(strings = {"sdk-success", "sdk-error", "sdk-cancel", "cancel-true", "cancel-false",
            "timeout", "timeout-value", "complete", "error", "async", "async-executor",
            "obtrude-value", "obtrude-error"})
    void everyCompletionPathEndsSpanBeforeObserversAndPreservesSdkOutcome(String outcome) throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracing = new WeaviateTracing("Article", new FITracer(provider.get("test"),
                    TraceConfig.builder().hideErrorDetails(true).build()));
            var source = new CompletableFuture<Object>();
            var traced = tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> source);
            var value = new Object();
            var failure = new IllegalArgumentException("private");
            var terminal = traced.handle((result, error) -> {
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                var span = exporter.getFinishedSpanItems().get(0);
                assertThat(span.getStatus().getStatusCode()).isEqualTo(error == null ? StatusCode.OK : StatusCode.ERROR);
                assertThat(span.getAttributes().toString() + span.getEvents()).doesNotContain("private");
                // The same result/error object reaches the caller, not a dependent
                // stage's CompletionException wrapper around a cancellation.
                if (error == null) assertThat(result).isSameAs(source.getNow(null));
                return null;
            });
            var observedError = traced.handle((result, error) -> error);
            var sourceError = source.handle((result, error) -> error);
            switch (outcome) {
                case "sdk-success" -> source.complete(value);
                case "sdk-error" -> source.completeExceptionally(failure);
                case "sdk-cancel" -> source.cancel(false);
                case "cancel-true" -> assertThat(traced.cancel(true)).isTrue();
                case "cancel-false" -> assertThat(traced.cancel(false)).isTrue();
                case "timeout" -> assertThat(traced.orTimeout(10, TimeUnit.MILLISECONDS)).isSameAs(traced);
                case "timeout-value" -> assertThat(traced.completeOnTimeout(value, 10, TimeUnit.MILLISECONDS)).isSameAs(traced);
                case "complete" -> assertThat(traced.complete(value)).isTrue();
                case "error" -> assertThat(traced.completeExceptionally(failure)).isTrue();
                case "async" -> assertThat(traced.completeAsync(() -> value)).isSameAs(traced);
                case "async-executor" -> assertThat(traced.completeAsync(() -> value, Runnable::run)).isSameAs(traced);
                case "obtrude-value" -> traced.obtrudeValue(value);
                case "obtrude-error" -> traced.obtrudeException(failure);
                default -> throw new AssertionError(outcome);
            }
            var error = observedError.get(5, TimeUnit.SECONDS);
            assertThat(error).isSameAs(sourceError.get(5, TimeUnit.SECONDS));
            assertThat(traced.isCancelled()).isEqualTo(source.isCancelled());
            terminal.get(5, TimeUnit.SECONDS);
            if (error == null) assertThat(traced.join()).isSameAs(value);
            assertThat(source.complete(new Object())).isFalse();
            assertThat(traced.complete(new Object())).isFalse();
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void forwardsCancellationFlagAndKeepsCancellationIdempotent(boolean interrupt) {
        var flags = new java.util.ArrayList<Boolean>();
        var source = new CompletableFuture<Object>() {
            @Override public boolean cancel(boolean mayInterruptIfRunning) {
                flags.add(mayInterruptIfRunning);
                return super.cancel(mayInterruptIfRunning);
            }
        };
        var tracer = new FITracer(io.opentelemetry.api.OpenTelemetry.noop().getTracer("test"), TraceConfig.builder().build());
        var traced = new WeaviateTracing("Article", tracer).callAsync("query", FISpanKind.RETRIEVER, null, () -> source);
        assertThat(traced.cancel(interrupt)).isTrue();
        assertThat(traced).isCancelled();
        assertThat(flags).containsExactly(interrupt);
        assertThat(traced.cancel(interrupt)).isTrue();
        assertThat(traced.complete(new Object())).isFalse();
    }

    @Test void concurrentCancellationAndSuccessPublishExactlyOneConsistentSpan() throws Exception {
        var exporter = InMemorySpanExporter.create();
        var pool = Executors.newFixedThreadPool(2);
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracing = new WeaviateTracing("Article", new FITracer(provider.get("test"), TraceConfig.builder().build()));
            for (int i = 0; i < 100; i++) {
                exporter.reset();
                var source = new CompletableFuture<Object>();
                var traced = tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> source);
                var start = new CountDownLatch(1);
                var terminal = traced.handle((value, error) -> {
                    assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                    assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode())
                            .isEqualTo(error == null ? StatusCode.OK : StatusCode.ERROR);
                    return null;
                });
                var success = pool.submit(() -> { start.await(); return source.complete(new Object()); });
                var cancellation = pool.submit(() -> { start.await(); return traced.cancel(true); });
                start.countDown();
                success.get(5, TimeUnit.SECONDS);
                cancellation.get(5, TimeUnit.SECONDS);
                terminal.get(5, TimeUnit.SECONDS);
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            }
        } finally {
            pool.shutdownNow();
        }
    }
}
