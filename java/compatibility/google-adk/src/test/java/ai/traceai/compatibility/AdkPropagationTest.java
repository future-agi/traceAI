package ai.traceai.compatibility;

import static org.junit.jupiter.api.Assertions.*;

import ai.traceai.nativeotel.GoogleAdkTelemetry;
import com.google.adk.telemetry.Tracing;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import io.reactivex.rxjava3.core.Flowable;
import io.reactivex.rxjava3.schedulers.Schedulers;
import java.util.Collection;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

class AdkPropagationTest {
    @Test void nativeRxJavaCancellationAndExplicitParentKeepOneSpanAndBaggage() throws Exception {
        List<SpanData> spans = new CopyOnWriteArrayList<>();
        SpanExporter exporter = new SpanExporter() {
            @Override public CompletableResultCode export(Collection<SpanData> batch) { spans.addAll(batch); return CompletableResultCode.ofSuccess(); }
            @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        OpenTelemetrySdk sdk = GoogleAdkTelemetry.register(new GoogleAdkTelemetry.Config(
                "http://localhost:4318", "key", "secret", "test-project"), exporter, false);
        Span parent = sdk.getTracer("test-parent").spanBuilder("request").startSpan();
        Context context = Context.root().with(parent).with(Baggage.builder()
                .put("session.id", "session-1").put("user.id", "user-1").build());
        try {
            CountDownLatch started = new CountDownLatch(1);
            CountDownLatch cancelled = new CountDownLatch(1);
            var subscription = Flowable.never().doOnSubscribe(ignored -> started.countDown())
                    .compose(Tracing.trace("invoke_agent cancelled").setParent(context))
                    .doFinally(cancelled::countDown).subscribeOn(Schedulers.computation()).test();
            assertTrue(started.await(5, TimeUnit.SECONDS));
            subscription.cancel();
            assertTrue(cancelled.await(5, TimeUnit.SECONDS));
            CountDownLatch completed = new CountDownLatch(1);
            String result = Flowable.just("result").subscribeOn(Schedulers.computation())
                    .compose(Tracing.<String>trace("call_llm").setParent(context))
                    .map(value -> {
                        assertEquals("session-1", Baggage.current().getEntryValue("session.id"));
                        Span child = Tracing.getTracer().spanBuilder("execute_tool local").startSpan();
                        child.end();
                        return value;
                    }).doFinally(completed::countDown).blockingFirst();
            assertEquals("result", result);
            assertTrue(completed.await(5, TimeUnit.SECONDS));
        } finally {
            parent.end();
            GoogleAdkTelemetry.shutdown(sdk);
        }
        assertEquals(4, spans.size());
        var nativeSpans = spans.stream().filter(s -> s.getInstrumentationScopeInfo().getName().equals("gcp.vertex.agent")).toList();
        assertEquals(3, nativeSpans.size());
        var llm = nativeSpans.stream().filter(s -> s.getName().equals("call_llm")).findFirst().orElseThrow();
        for (var span : nativeSpans) {
            assertEquals(parent.getSpanContext().getTraceId(), span.getTraceId());
            assertEquals("session-1", span.getAttributes().get(AttributeKey.stringKey("session.id")));
            assertEquals("user-1", span.getAttributes().get(AttributeKey.stringKey("user.id")));
            assertEquals(span.getName().startsWith("execute_tool") ? llm.getSpanId() : parent.getSpanContext().getSpanId(), span.getParentSpanId());
        }
    }
}
