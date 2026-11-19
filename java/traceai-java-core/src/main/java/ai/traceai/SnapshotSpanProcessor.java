package ai.traceai;

import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * A synchronous span processor, like the SDK's {@code SimpleSpanProcessor}, whose flush cannot hang.
 *
 * <p>{@code SimpleSpanProcessor.forceFlush()} hands its live pending-export set to
 * {@code CompletableResultCode.ofAll}, which sizes a countdown from {@code size()} and then iterates. An
 * export that completes in between is removed and never registers, so the flush result never completes
 * (open-telemetry/opentelemetry-java#8995; the fix, PR #9005, is not released as of 1.68.0, and
 * TraceAI supports 1.35.0 and later). This processor exports on the ending thread exactly as the SDK
 * one does, but flushes a snapshot of the pending exports, which cannot change under the countdown.
 */
final class SnapshotSpanProcessor implements SpanProcessor {
    private static final Logger LOGGER = Logger.getLogger(SnapshotSpanProcessor.class.getName());

    private final SpanExporter exporter;
    private final Set<CompletableResultCode> pending = Collections.newSetFromMap(new ConcurrentHashMap<>());
    private final AtomicBoolean shutdown = new AtomicBoolean();

    SnapshotSpanProcessor(SpanExporter exporter) {
        this.exporter = exporter;
    }

    @Override
    public boolean isStartRequired() {
        return false;
    }

    @Override
    public void onStart(Context parentContext, ReadWriteSpan span) {
        // Nothing to do on start.
    }

    @Override
    public boolean isEndRequired() {
        return true;
    }

    @Override
    public void onEnd(ReadableSpan span) {
        if (!span.getSpanContext().isSampled()) {
            return;
        }
        try {
            CompletableResultCode result = exporter.export(Collections.singletonList(span.toSpanData()));
            pending.add(result);
            result.whenComplete(() -> {
                pending.remove(result);
                if (!result.isSuccess()) {
                    LOGGER.log(Level.FINE, "Exporter failed");
                }
            });
        } catch (RuntimeException e) {
            // An exporter failure must never reach the traced application.
            LOGGER.log(Level.WARNING, "Exporter threw an Exception", e);
        }
    }

    @Override
    public CompletableResultCode forceFlush() {
        List<CompletableResultCode> snapshot = new ArrayList<>(pending);
        return CompletableResultCode.ofAll(snapshot);
    }

    @Override
    public CompletableResultCode shutdown() {
        if (!shutdown.compareAndSet(false, true)) {
            return CompletableResultCode.ofSuccess();
        }
        CompletableResultCode result = new CompletableResultCode();
        CompletableResultCode flush = forceFlush();
        flush.whenComplete(() -> {
            CompletableResultCode closed = exporter.shutdown();
            closed.whenComplete(() -> {
                if (flush.isSuccess() && closed.isSuccess()) {
                    result.succeed();
                } else {
                    result.fail();
                }
            });
        });
        return result;
    }
}
