package ai.traceai.weaviate;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import io.weaviate.client6.v1.api.collections.query.FetchObjects;
import org.junit.jupiter.api.Test;

import java.util.Collection;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.*;

class WeaviateAsyncLifecycleTest {
    static final class Capture implements SpanExporter {
        final List<SpanData> spans = new CopyOnWriteArrayList<>();
        volatile boolean stopped;
        public CompletableResultCode export(Collection<SpanData> data) {
            if (stopped) return CompletableResultCode.ofFailure();
            spans.addAll(data);
            return CompletableResultCode.ofSuccess();
        }
        public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
        public CompletableResultCode shutdown() { stopped = true; return CompletableResultCode.ofSuccess(); }
    }
    static final class EndMonitor implements SpanProcessor {
        final CountDownLatch ended = new CountDownLatch(1);
        public void onStart(Context c, ReadWriteSpan s) {}
        public boolean isStartRequired() { return false; }
        public void onEnd(ReadableSpan s) { ended.countDown(); }
        public boolean isEndRequired() { return true; }
    }
    static void waitForRequest(WeaviateV6Fixture server) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (server.pendingSearch == null && System.nanoTime() < deadline) Thread.sleep(5);
        assertThat(server.pendingSearch).isNotNull();
    }
    @Test void callerCompletionAndShutdownMustNotLoseFinishedQuery() throws Exception {
        var capture = new Capture();
        var monitor = new EndMonitor();
        try (var provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(capture)).addSpanProcessor(monitor).build();
             var server = new WeaviateV6Fixture(); var client = server.asyncClient()) {
            server.holdSearch = true;
            var traced = TracedWeaviateCollectionAsync.of(client, "Article", new FITracer(provider.get("recall"),
                    TraceConfig.builder().hideErrorDetails(true).build()));
            var query = traced.fetchObjects(FetchObjects.of(q -> q.limit(5)));
            waitForRequest(server);
            var countAtCompletion = new AtomicInteger(-1);
            var terminal = query.thenRun(() -> {
                countAtCompletion.set(capture.spans.size());
                provider.forceFlush().join(5, TimeUnit.SECONDS);
                provider.shutdown().join(5, TimeUnit.SECONDS);
            });
            server.pendingSearch.onNext(WeaviateV6Fixture.searchReply(false));
            server.pendingSearch.onCompleted();
            terminal.get(5, TimeUnit.SECONDS);
            assertThat(monitor.ended.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(countAtCompletion.get()).isEqualTo(1);
            assertThat(capture.spans).as("completed query survives an immediate flush/shutdown in its continuation").hasSize(1);
        }
    }
    @Test void userContinuationMustNotExtendQuerySpan() throws Exception {
        var capture = new Capture();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(capture)).build();
             var server = new WeaviateV6Fixture(); var client = server.asyncClient()) {
            server.holdSearch = true;
            var traced = TracedWeaviateCollectionAsync.of(client, "Article", new FITracer(provider.get("recall"), TraceConfig.builder().build()));
            var query = traced.fetchObjects(FetchObjects.of());
            waitForRequest(server);
            var terminal = query.thenRun(() -> {
                entered.countDown();
                try { release.await(5, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            });
            server.pendingSearch.onNext(WeaviateV6Fixture.searchReply(false));
            server.pendingSearch.onCompleted();
            try {
                assertThat(entered.await(5, TimeUnit.SECONDS)).isTrue();
                assertThat(query).isCompleted();
                assertThat(capture.spans).as("query span is finished before caller result processing").hasSize(1);
            } finally {
                release.countDown();
                terminal.get(5, TimeUnit.SECONDS);
            }
        }
    }
    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings = {"cancel", "timeout"})
    void unsuccessfulCompletionMustExportBeforeCallerShutdown(String outcome) throws Exception {
        var capture = new Capture();
        var monitor = new EndMonitor();
        try (var provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(capture)).addSpanProcessor(monitor).build();
             var server = new WeaviateV6Fixture(); var client = server.asyncClient()) {
            server.holdSearch = true;
            var traced = TracedWeaviateCollectionAsync.of(client, "Article", new FITracer(provider.get("recall"),
                    TraceConfig.builder().hideErrorDetails(true).build()));
            var query = traced.fetchObjects(FetchObjects.of());
            waitForRequest(server);
            var terminal = query.handle((result, error) -> {
                assertThat(error).isNotNull();
                provider.forceFlush().join(5, TimeUnit.SECONDS);
                provider.shutdown().join(5, TimeUnit.SECONDS);
                return null;
            });
            if (outcome.equals("cancel")) assertThat(query.cancel(true)).isTrue();
            else query.orTimeout(100, TimeUnit.MILLISECONDS);
            terminal.get(5, TimeUnit.SECONDS);
            assertThat(monitor.ended.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(capture.spans).as("failed query survives immediate flush/shutdown in its continuation").hasSize(1);
        }
    }
    @Test void timeoutRemainsAnErrorWithoutPrivateContent() throws Exception {
        var capture = new Capture();
        var monitor = new EndMonitor();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(capture)).addSpanProcessor(monitor).build();
             var server = new WeaviateV6Fixture(); var client = server.asyncClient()) {
            server.holdSearch = true;
            var traced = TracedWeaviateCollectionAsync.of(client, "Article", new FITracer(provider.get("recall"),
                    TraceConfig.builder().hideInputs(true).hideOutputs(true).hideErrorDetails(true).build()));
            var query = traced.fetchObjects(FetchObjects.of()).orTimeout(250, TimeUnit.MILLISECONDS);
            assertThatThrownBy(() -> query.get(5, TimeUnit.SECONDS)).hasCauseInstanceOf(TimeoutException.class);
            assertThat(monitor.ended.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(capture.spans).hasSize(1);
            assertThat(capture.spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(capture.spans.get(0).getAttributes().get(AttributeKey.stringKey("error.type"))).isEqualTo(TimeoutException.class.getName());
            assertThat(capture.spans.get(0).toString()).doesNotContain(WeaviateV6Fixture.PRIVATE);
        }
    }
}
