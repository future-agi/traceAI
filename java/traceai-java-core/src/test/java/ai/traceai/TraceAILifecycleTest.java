package ai.traceai;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.*;

class TraceAILifecycleTest {
    @BeforeEach
    void reset() {
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
    }

    @AfterEach
    void cleanup() {
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
    }

    @Test
    void initializesAlongsideAnExistingGlobal() {
        GlobalOpenTelemetry.set(OpenTelemetry.noop());
        OpenTelemetry existing = GlobalOpenTelemetry.get();
        assertThatCode(() -> TraceAI.init(config())).doesNotThrowAnyException();
        assertThat(TraceAI.isInitialized()).isTrue();
        assertThat(GlobalOpenTelemetry.get()).isSameAs(existing);
    }

    @Test
    void canInitializeAgainAfterShutdown() {
        TraceAI.init(config());
        FITracer first = TraceAI.getTracer();
        TraceAI.shutdown();
        assertThat(TraceAI.isInitialized()).isFalse();
        assertThatThrownBy(TraceAI::getTracer).isInstanceOf(IllegalStateException.class);
        Span stopped = first.startSpan("stopped", FISpanKind.CHAIN);
        assertThat(stopped.isRecording()).isFalse();
        stopped.end();

        assertThatCode(() -> TraceAI.init(config())).doesNotThrowAnyException();
        assertThat(TraceAI.isInitialized()).isTrue();
        assertThat(TraceAI.getTracer()).isNotSameAs(first);
        TraceAI.getTracer().startSpan("restarted", FISpanKind.CHAIN).end();
    }

    @Test
    void leavesGlobalUnregisteredForApplicationStartup() {
        TraceAI.init(config());
        TraceAI.shutdown();
        // Even calling GlobalOpenTelemetry.get() would lock in a no-op global at the API floor.
        assertThatCode(() -> GlobalOpenTelemetry.set(OpenTelemetry.noop())).doesNotThrowAnyException();
    }

    @Test
    void standaloneProviderDoesNotExportThroughOrCloseTheGlobalProvider() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        LifecycleCounter lifecycle = new LifecycleCounter();
        try (SdkTracerProvider application = provider(exporter, lifecycle)) {
            OpenTelemetrySdk.builder().setTracerProvider(application).buildAndRegisterGlobal();
            OpenTelemetry global = GlobalOpenTelemetry.get();
            TraceAI.init(config());
            TraceAI.getTracer().startSpan("standalone", FISpanKind.CHAIN).end();
            TraceAI.shutdown();
            assertThat(GlobalOpenTelemetry.get()).isSameAs(global);
            assertThat(lifecycle.flushes).isZero();
            assertThat(lifecycle.shutdowns).isZero();
            global.getTracer("application").spanBuilder("still-running").startSpan().end();
            assertThat(exporter.getFinishedSpanItems()).extracting(span -> span.getName())
                .containsExactly("still-running");
        }
    }

    @Test
    void borrowsGlobalSdkWithoutDuplicateSpansAndPreservesParentageAndPrivacy() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        LifecycleCounter lifecycle = new LifecycleCounter();
        try (SdkTracerProvider application = provider(exporter, lifecycle)) {
            OpenTelemetrySdk.builder().setTracerProvider(application).buildAndRegisterGlobal();
            OpenTelemetry global = GlobalOpenTelemetry.get();
            // Invalid local-export configuration must not create an exporter in borrowed mode.
            TraceConfig config = TraceConfig.builder().baseUrl("not-an-endpoint").apiKey("synthetic")
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build();
            TraceAI.init(config, global);
            FITracer tracer = TraceAI.getTracer();
            Span parent = global.getTracer("application").spanBuilder("parent").startSpan();
            try (Scope ignored = parent.makeCurrent()) {
                Span child = tracer.startSpan("child", FISpanKind.LLM);
                tracer.setInputValue(child, "private-input");
                tracer.setOutputValue(child, "private-output");
                tracer.setError(child, new IllegalStateException("private-error"));
                child.end();
            }
            parent.end();
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
            var child = exporter.getFinishedSpanItems().get(0);
            assertThat(child.getName()).isEqualTo("child");
            assertThat(child.getTraceId()).isEqualTo(parent.getSpanContext().getTraceId());
            assertThat(child.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(child.getInstrumentationScopeInfo().getName()).isEqualTo("traceai-java");
            assertThat(child.getInstrumentationScopeInfo().getVersion()).isEqualTo(TraceAI.getVersion());
            assertThat(child.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
                .isEqualTo("LLM");
            assertThat(child.getAttributes().toString()).doesNotContain("private-input", "private-output");
            assertThat(child.getStatus().getDescription()).isEmpty();
            assertThat(child.getEvents().toString()).doesNotContain("private-error");

            TraceAI.shutdown();
            assertThat(lifecycle.flushes).isZero();
            assertThat(lifecycle.shutdowns).isZero();
            assertThat(GlobalOpenTelemetry.get()).isSameAs(global);
            TraceAI.init(config, global);
            TraceAI.getTracer().startSpan("reattached", FISpanKind.CHAIN).end();
            TraceAI.shutdown();
            global.getTracer("application").spanBuilder("after-detach").startSpan().end();
            assertThat(exporter.getFinishedSpanItems()).extracting(span -> span.getName())
                .containsExactly("child", "parent", "reattached", "after-detach");
        }
    }

    @Test
    void firstInitializationWinsAcrossBothOverloads() {
        TraceAI.init(config());
        FITracer owned = TraceAI.getTracer();
        TraceAI.init(config());
        TraceAI.init(config(), OpenTelemetry.noop());
        assertThat(TraceAI.getTracer()).isSameAs(owned);
        TraceAI.shutdown();

        TraceAI.init(config(), OpenTelemetry.noop());
        FITracer borrowed = TraceAI.getTracer();
        TraceAI.init(config());
        TraceAI.init(config(), OpenTelemetry.noop());
        assertThat(TraceAI.getTracer()).isSameAs(borrowed);
        TraceAI.shutdown();
        TraceAI.shutdown();
        TraceAI.init(config());
        assertThat(TraceAI.getTracer()).isNotSameAs(borrowed);
        TraceAI.getTracer().startSpan("owned-again", FISpanKind.CHAIN).end();
    }

    @Test
    void borrowingANonGlobalSdkDoesNotRegisterIt() {
        try (SdkTracerProvider application = SdkTracerProvider.builder().build()) {
            TraceAI.init(config(), OpenTelemetrySdk.builder().setTracerProvider(application).build());
            assertThatCode(() -> GlobalOpenTelemetry.set(OpenTelemetry.noop())).doesNotThrowAnyException();
            TraceAI.shutdown();
        }
    }

    @Test
    void invalidArgumentsLeaveInitializationRetryable() {
        assertThatIllegalArgumentException().isThrownBy(() -> TraceAI.init(null));
        assertThatIllegalArgumentException().isThrownBy(() -> TraceAI.init(config(), null));
        assertThatIllegalArgumentException().isThrownBy(() -> TraceAI.init(null, OpenTelemetry.noop()));
        assertThat(TraceAI.isInitialized()).isFalse();
        TraceAI.init(config(), OpenTelemetry.noop());
        assertThat(TraceAI.isInitialized()).isTrue();
    }

    private static TraceConfig config() {
        return TraceConfig.builder().projectName("offline-lifecycle").build();
    }

    private static SdkTracerProvider provider(InMemorySpanExporter exporter, LifecycleCounter lifecycle) {
        return SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .addSpanProcessor(lifecycle).build();
    }

    private static final class LifecycleCounter implements SpanProcessor {
        private int flushes;
        private int shutdowns;
        @Override public void onStart(Context parentContext, ReadWriteSpan span) {}
        @Override public boolean isStartRequired() { return false; }
        @Override public void onEnd(ReadableSpan span) {}
        @Override public boolean isEndRequired() { return false; }
        @Override public CompletableResultCode forceFlush() {
            flushes++;
            return CompletableResultCode.ofSuccess();
        }
        @Override public CompletableResultCode shutdown() {
            shutdowns++;
            return CompletableResultCode.ofSuccess();
        }
    }
}
