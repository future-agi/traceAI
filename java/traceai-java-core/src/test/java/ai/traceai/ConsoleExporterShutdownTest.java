package ai.traceai;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.Span;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Level;
import java.util.logging.Logger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * With the console exporter on, TraceAI.shutdown() must not wait out its 10 s join budget because
 * spans end while it flushes. OpenTelemetry SDK 1.35.0 through 1.68.0
 * {@code SimpleSpanProcessor.forceFlush()} can return a result that never completes when an export
 * finishes while it counts the pending set (open-telemetry/opentelemetry-java#8995, fix PR #9005 unmerged).
 */
class ConsoleExporterShutdownTest {
    private static final int ROUNDS = 40;
    private static final long STALL_NANOS = 5_000_000_000L;
    private final Logger exporterLog = Logger.getLogger("io.opentelemetry.exporter.logging.LoggingSpanExporter");
    private Level previousLevel;

    @BeforeEach
    void silenceConsoleExporter() {
        previousLevel = exporterLog.getLevel();
        exporterLog.setLevel(Level.OFF);
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
    }

    @AfterEach
    void restore() {
        TraceAI.shutdown();
        GlobalOpenTelemetry.resetForTest();
        exporterLog.setLevel(previousLevel);
    }

    @Test
    void shutdownDoesNotStallWhileSpansEndConcurrently() throws Exception {
        TraceConfig config = TraceConfig.builder().projectName("console-shutdown").enableConsoleExporter(true).build();
        int threads = Math.max(8, Runtime.getRuntime().availableProcessors());
        long worst = 0;
        int stalled = 0;
        for (int round = 0; round < ROUNDS; round++) {
            TraceAI.init(config);
            FITracer tracer = TraceAI.getTracer();
            AtomicBoolean stop = new AtomicBoolean();
            List<Thread> workers = new ArrayList<>();
            for (int i = 0; i < threads; i++) {
                Thread worker = new Thread(() -> {
                    while (!stop.get()) {
                        Span span = tracer.startSpan("busy", FISpanKind.CHAIN);
                        span.end();
                    }
                }, "span-ender-" + i);
                worker.setDaemon(true);
                worker.start();
                workers.add(worker);
            }
            Thread.sleep(2);
            long start = System.nanoTime();
            TraceAI.shutdown();
            long elapsed = System.nanoTime() - start;
            stop.set(true);
            for (Thread worker : workers) {
                worker.join();
            }
            worst = Math.max(worst, elapsed);
            if (elapsed > STALL_NANOS) {
                stalled++;
            }
        }
        assertThat(stalled)
            .as("shutdowns slower than 5 s out of %d rounds (worst %d ms)", ROUNDS, worst / 1_000_000)
            .isZero();
    }
}
