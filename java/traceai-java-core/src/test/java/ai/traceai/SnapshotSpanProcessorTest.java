package ai.traceai;

import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import java.util.Collection;
import java.util.concurrent.TimeUnit;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Handler;
import java.util.logging.Level;
import java.util.logging.LogRecord;
import java.util.logging.Logger;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

class SnapshotSpanProcessorTest {
    @Test
    void exportsEachSampledSpanSynchronouslyAndFlushCompletes() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(new SnapshotSpanProcessor(exporter)).build()) {
            provider.get("t").spanBuilder("one").startSpan().end();
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(provider.forceFlush().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        }
    }

    @Test
    void doesNotExportUnsampledSpans() {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(Sampler.alwaysOff())
            .addSpanProcessor(new SnapshotSpanProcessor(exporter)).build()) {
            provider.get("t").spanBuilder("dropped").startSpan().end();
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
        }
    }

    @Test
    void flushWaitsForAnExportThatIsStillPending() {
        CompletableResultCode slow = new CompletableResultCode();
        SpanExporter exporter = new SpanExporter() {
            public CompletableResultCode export(Collection<SpanData> spans) { return slow; }
            public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        SnapshotSpanProcessor processor = new SnapshotSpanProcessor(exporter);
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(processor).build()) {
            provider.get("t").spanBuilder("slow").startSpan().end();
            CompletableResultCode flush = processor.forceFlush();
            assertThat(flush.isDone()).isFalse();
            slow.succeed();
            assertThat(flush.join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        }
    }

    @Test
    void shutdownIsIdempotentAndShutsTheExporterDownOnce() {
        AtomicInteger shutdowns = new AtomicInteger();
        SpanExporter exporter = new SpanExporter() {
            public CompletableResultCode export(Collection<SpanData> spans) { return CompletableResultCode.ofSuccess(); }
            public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            public CompletableResultCode shutdown() { shutdowns.incrementAndGet(); return CompletableResultCode.ofSuccess(); }
        };
        SnapshotSpanProcessor processor = new SnapshotSpanProcessor(exporter);
        assertThat(processor.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        assertThat(processor.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
        assertThat(shutdowns).hasValue(1);
    }

    @Test
    void anExporterFailureNeverReachesTheApplication() {
        SpanExporter exporter = new SpanExporter() {
            public CompletableResultCode export(Collection<SpanData> spans) { throw new IllegalStateException("boom"); }
            public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(new SnapshotSpanProcessor(exporter)).build()) {
            assertThatCode(() -> provider.get("t").spanBuilder("x").startSpan().end()).doesNotThrowAnyException();
        }
    }

    private static List<LogRecord> capture(Runnable body) {
        Logger logger = Logger.getLogger(SnapshotSpanProcessor.class.getName());
        List<LogRecord> records = new CopyOnWriteArrayList<>();
        Handler handler = new Handler() {
            public void publish(LogRecord r) { records.add(r); }
            public void flush() { }
            public void close() { }
        };
        handler.setLevel(Level.ALL);
        Level previous = logger.getLevel();
        boolean useParent = logger.getUseParentHandlers();
        logger.setLevel(Level.ALL);
        logger.setUseParentHandlers(false);
        logger.addHandler(handler);
        try {
            body.run();
        } finally {
            logger.removeHandler(handler);
            logger.setUseParentHandlers(useParent);
            logger.setLevel(previous);
        }
        return records;
    }

    @Test
    void anExporterExceptionIsLoggedAsOneWarningWithTheThrowable() {
        IllegalStateException boom = new IllegalStateException("boom");
        SpanExporter exporter = new SpanExporter() {
            public CompletableResultCode export(Collection<SpanData> spans) { throw boom; }
            public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        List<LogRecord> records = capture(() -> {
            try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(new SnapshotSpanProcessor(exporter)).build()) {
                provider.get("t").spanBuilder("secret-span").startSpan().end();
            }
        });
        assertThat(records).hasSize(1);
        assertThat(records.get(0).getLevel()).isEqualTo(Level.WARNING);
        assertThat(records.get(0).getMessage()).isEqualTo("Exporter threw an Exception");
        assertThat(records.get(0).getThrown()).isSameAs(boom);
    }

    @Test
    void aFailedExportResultIsLoggedAtFine() {
        SpanExporter exporter = new SpanExporter() {
            public CompletableResultCode export(Collection<SpanData> spans) { return CompletableResultCode.ofFailure(); }
            public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        List<LogRecord> records = capture(() -> {
            try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(new SnapshotSpanProcessor(exporter)).build()) {
                provider.get("t").spanBuilder("secret-span").startSpan().end();
            }
        });
        assertThat(records).hasSize(1);
        assertThat(records.get(0).getLevel()).isEqualTo(Level.FINE);
        assertThat(records.get(0).getMessage()).isEqualTo("Exporter failed");
        assertThat(records.get(0).getThrown()).isNull();
    }
}
