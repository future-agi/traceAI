package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.weaviate.client6.v1.api.collections.query.FetchObjects;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.concurrent.Callable;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.*;

class WeaviateCancellationTest {
    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void concurrentCancellationWaitsForSpanAndPublishesTerminalState(boolean interrupt) throws Exception {
        var ending = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var secondStarted = new CountDownLatch(1);
        var exporter = InMemorySpanExporter.create();
        SpanProcessor gatedProcessor = new SpanProcessor() {
            public void onStart(Context context, ReadWriteSpan span) {}
            public boolean isStartRequired() { return false; }
            public void onEnd(ReadableSpan span) {
                ending.countDown();
                await(release);
            }
            public boolean isEndRequired() { return true; }
        };
        var pool = Executors.newFixedThreadPool(2);
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(gatedProcessor)
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
             var server = new WeaviateV6Fixture(); var client = server.asyncClient()) {
            server.holdSearch = true;
            var traced = TracedWeaviateCollectionAsync.of(client, "Article",
                    new FITracer(provider.get("cancel-contract"), TraceConfig.builder().hideErrorDetails(true).build()));
            var query = traced.fetchObjects(FetchObjects.of());
            WeaviateAsyncLifecycleTest.waitForRequest(server);
            var terminal = query.handle((result, error) -> {
                assertThat(error).isInstanceOf(CancellationException.class);
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                return null;
            });
            var firstCancellation = pool.submit(() -> query.cancel(interrupt));
            try {
                assertThat(ending.await(5, TimeUnit.SECONDS)).isTrue();
                var secondCancellation = pool.submit(() -> {
                    secondStarted.countDown();
                    boolean cancelled = query.cancel(interrupt);
                    assertThat(cancelled).isTrue();
                    assertThat(query.isCancelled()).as("successful cancel must publish cancellation before returning").isTrue();
                    assertThat(query.isDone()).isTrue();
                    assertThatThrownBy(() -> query.get(100, TimeUnit.MILLISECONDS)).isInstanceOf(CancellationException.class);
                    return cancelled;
                });
                assertThat(secondStarted.await(5, TimeUnit.SECONDS)).isTrue();
                // Cancellation cannot return successfully or run continuations while
                // telemetry is still finalizing, even when the SDK is already cancelled.
                assertThatThrownBy(() -> secondCancellation.get(100, TimeUnit.MILLISECONDS))
                        .isInstanceOf(TimeoutException.class);
                assertThat(query.isDone()).isFalse();
                assertThat(terminal.isDone()).isFalse();
                release.countDown();
                assertThat(secondCancellation.get(5, TimeUnit.SECONDS)).isTrue();
                assertThat(firstCancellation.get(5, TimeUnit.SECONDS)).isTrue();
                terminal.get(5, TimeUnit.SECONDS);
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            } finally {
                release.countDown();
            }
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test void repeatedCancellationDoesNotWaitForUserContinuations() throws Exception {
        var continuationEntered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var pool = Executors.newFixedThreadPool(2);
        try {
            var source = new CompletableFuture<Object>();
            var query = noopTracing().callAsync("query", FISpanKind.RETRIEVER, null, () -> source);
            var terminal = query.handle((result, error) -> {
                continuationEntered.countDown();
                await(release);
                return null;
            });
            var firstCancellation = pool.submit(() -> query.cancel(true));
            assertThat(continuationEntered.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(firstCancellation.isDone()).isFalse();
            var secondCancellation = pool.submit(() -> query.cancel(false));
            assertThat(secondCancellation.get(5, TimeUnit.SECONDS)).isTrue();
            assertThat(query.isCancelled()).isTrue();
            assertThat(query.isDone()).isTrue();
            release.countDown();
            assertThat(firstCancellation.get(5, TimeUnit.SECONDS)).isTrue();
            terminal.get(5, TimeUnit.SECONDS);
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test void concurrentCancelKeepsTerminalStateWithNoopTelemetry() throws Exception {
        var pool = Executors.newFixedThreadPool(2);
        var tracing = noopTracing();
        int prematureReturns = 0;
        try {
            for (int i = 0; i < 10_000; i++) {
                var source = new CompletableFuture<Object>();
                var query = tracing.callAsync("query", FISpanKind.RETRIEVER, null, () -> source);
                var start = new CountDownLatch(1);
                Callable<Boolean> cancel = () -> {
                    start.await();
                    boolean cancelled = query.cancel(true);
                    return cancelled && (!query.isCancelled() || !query.isDone());
                };
                var first = pool.submit(cancel);
                var second = pool.submit(cancel);
                start.countDown();
                if (first.get(5, TimeUnit.SECONDS)) prematureReturns++;
                if (second.get(5, TimeUnit.SECONDS)) prematureReturns++;
            }
        } finally {
            pool.shutdownNow();
        }
        assertThat(prematureReturns).as("successful cancellations before terminal state").isZero();
    }

    private static WeaviateTracing noopTracing() {
        return new WeaviateTracing("Article", new FITracer(OpenTelemetry.noop().getTracer("cancel-contract"),
                TraceConfig.builder().build()));
    }

    private static void await(CountDownLatch latch) {
        try {
            assertThat(latch.await(10, TimeUnit.SECONDS)).as("test gate released").isTrue();
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new AssertionError(error);
        }
    }
}
