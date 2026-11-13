package ai.traceai.auto;

import io.opentelemetry.sdk.common.CompletableResultCode;
import java.time.Duration;
import java.util.concurrent.*;
import java.util.function.Supplier;

/** A hostile processor/exporter cannot hold the caller, JVM shutdown hook or an agent monitor. */
final class Lifecycle {
    private static final ThreadLocal<Boolean> INSIDE = ThreadLocal.withInitial(() -> false);
    private static final ExecutorService WORKER = new ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<>(16), task -> { Thread thread = new Thread(task, "traceai-auto-lifecycle"); thread.setDaemon(true); return thread; }, new ThreadPoolExecutor.AbortPolicy());
    // Cleanup is queued once per owned runtime, independently of user-triggered flushes.
    private static final ExecutorService CLEANUP = Executors.newSingleThreadExecutor(task -> { Thread thread = new Thread(task, "traceai-auto-cleanup"); thread.setDaemon(true); return thread; });
    static void cleanup(Supplier<CompletableResultCode> action, Duration timeout, boolean wait) { dispatch(action, timeout, wait && !INSIDE.get(), CLEANUP); }
    static void run(Supplier<CompletableResultCode> action, Duration timeout, boolean wait) {
        if (INSIDE.get()) return;
        dispatch(action, timeout, wait, WORKER);
    }
    private static void dispatch(Supplier<CompletableResultCode> action, Duration timeout, boolean wait, ExecutorService worker) {
        if (timeout == null || timeout.isNegative() || timeout.isZero()) {
            // A zero wait budget must not abandon owned resources.
            if (worker != CLEANUP) return;
            timeout = Duration.ofNanos(1); wait = false;
        }
        long nanos;
        try { nanos = timeout.toNanos(); } catch (ArithmeticException ignored) { nanos = TimeUnit.SECONDS.toNanos(5); }
        long deadline = System.nanoTime() + nanos;
        try {
            Future<?> task = worker.submit(() -> {
                INSIDE.set(true);
                try {
                    // The caller's deadline bounds waiting, not ownership of queued cleanup.
                    // In particular, shutdown must still execute after an earlier callback unblocks.
                    CompletableResultCode result = action.get();
                    long remaining = deadline - System.nanoTime(); if (remaining > 0 && result != null) result.join(remaining, TimeUnit.NANOSECONDS);
                } catch (Throwable ignored) { } finally { INSIDE.remove(); }
            });
            if (wait) {
                try { task.get(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS); }
                catch (TimeoutException ignored) { /* Do not cancel cleanup that has not started yet. */ }
            }
        } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
        catch (Throwable ignored) { }
    }
    private Lifecycle() {}
}
