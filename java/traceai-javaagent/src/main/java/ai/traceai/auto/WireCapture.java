package ai.traceai.auto;

import java.nio.ByteBuffer;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;

final class WireCapture {
    final WireCore.Request request;
    final WireCore.Response response = new WireCore.Response();
    final WireCore.Match match;
    final OtelBridge.Snapshot context;
    final TraceAIAuto.RuntimeState runtime;
    final long startEpochNanos;
    final long start;
    volatile long endEpochNanos, lastByteNanos, transportByteNanos, firstErrorNanos;
    void errorObserved(long observed) { if (firstErrorNanos == 0) firstErrorNanos = observed; }
    volatile BodyCapture input;
    private volatile BodyCapture output;
    private volatile boolean requestComplete;
    volatile boolean transportReturned;
    private final int limit;
    private long transportSize;
    private boolean transportTimeline;
    private final AtomicBoolean finished = new AtomicBoolean();
    private volatile Throwable transportFailure, networkFailure;
    WireCapture(WireCore.Request request, Map<String, String> headers, WireCore.Match match, OtelBridge.Snapshot context, int limit) {
        this(request, headers, match, context, limit, null);
    }
    WireCapture(WireCore.Request request, Map<String, String> headers, WireCore.Match match, OtelBridge.Snapshot context, int limit, TraceAIAuto.RuntimeState runtime) {
        this.request = request; request.headers = new LinkedHashMap<>(headers); this.match = match; this.context = context; this.limit = limit; this.runtime = runtime;
        KnownSecrets.observe(headers, request.url);
        request.limit = limit; response.limit = limit;
        startEpochNanos = context.startEpochNanos(); start = context.startNanos();
        input = new BodyCapture(captureType(headers), limit, false);
    }
    private static String captureType(Map<String, String> headers) {
        String type = WireCore.rawHeader(headers, "content-type");
        return encoded(headers) && !WireCore.binary(WireCore.contentType(headers)) ? "application/json" : type;
    }
    private static boolean encoded(Map<String, String> headers) {
        String value = WireCore.rawHeader(headers, "content-encoding"); return !value.isBlank() && !value.equalsIgnoreCase("identity");
    }
    void requestStarted() {
        try {
            if (finished.get()) return;
            BodyCapture previous = input;
            input = new BodyCapture(captureType(request.headers), limit, false); requestComplete = false;
            previous.release();
        } catch (Throwable ignored) { }
    }
    void requestBytes(ByteBuffer data) { try { if (!finished.get()) input.add(data); } catch (Throwable ignored) { } }
    void requestComplete() { requestComplete = true; }
    void responseHeaders(int status, Map<String, String> headers) {
        try {
            if (finished.get()) return;
            if (response.status != null && response.status == status && response.headers.equals(headers) && output != null) return;
            response.arrivalLost |= encoded(response.headers) || encoded(headers);
            response.status = status; response.headers = new LinkedHashMap<>(headers);
            response.arrivals = new StreamFrames.Timeline(); response.firstChunkSeconds = null; response.firstTextSeconds = null;
            transportSize = 0; transportTimeline = false;
            lastByteNanos = 0; transportByteNanos = 0;
            BodyCapture previous = output;
            if (previous != null) previous.release();
            output = new BodyCapture(captureType(headers), limit,
                !encoded(headers) && "text_to_speech".equals(match.endpoint().get("operation")));
        } catch (Throwable ignored) { }
    }
    void transportBytes(long count, long observed) {
        if (finished.get() || count <= 0) return;
        transportByteNanos = observed; transportSize += count;
        if (response.firstChunkSeconds == null) response.firstChunkSeconds = Math.max(0, observed - start) / 1e9;
        BodyCapture capture = output;
        // Ordinary OkHttp bodies preserve the original transport byte offsets even when
        // application consumption coalesces buffered reads. Audio has stripped payload offsets.
        if (capture != null && capture.audio == null && response.arrivals != null) {
            transportTimeline = true; response.arrivals.add(transportSize, (observed - start) / 1e9);
        }
    }
    void responseBytes(ByteBuffer data) { responseBytes(data, System.nanoTime()); }
    void responseBytes(ByteBuffer data, long observed) {
        try {
            if (finished.get()) return;
            if (data.hasRemaining()) lastByteNanos = observed;
            if (data.hasRemaining() && response.firstChunkSeconds == null) response.firstChunkSeconds = (lastByteNanos - start) / 1e9;
            BodyCapture capture = output;
            if (capture != null) {
                boolean nonempty = data.hasRemaining(); capture.add(data);
                if (nonempty && !transportTimeline && response.arrivals != null)
                    response.arrivals.add(capture.audio == null ? capture.size : capture.audio.sanitized.size(), (observed - start) / 1e9);
            }
        } catch (Throwable ignored) { }
    }
    void responseCount(long count) { responseCount(count, System.nanoTime()); }
    void responseCount(long count, long observed) {
        try {
            if (finished.get()) return;
            if (count > 0) lastByteNanos = observed;
            if (count > 0 && response.firstChunkSeconds == null) response.firstChunkSeconds = (lastByteNanos - start) / 1e9;
            if (output != null) output.size += count;
        } catch (Throwable ignored) { }
    }
    private boolean completeLength() {
        try {
            String[] values = WireCore.rawHeader(response.headers, "content-length").trim().split("[,\\r\\n]+", -1);
            long expected = Long.parseLong(values[0].trim());
            for (String value : values) if (Long.parseLong(value.trim()) != expected) return false;
            BodyCapture capture = output;
            return expected >= 0 && capture != null && capture.size == expected;
        } catch (RuntimeException ignored) { return false; }
    }
    void close() { finish(null, !completeLength()); }
    void transportFailure(Throwable error) { if (!finished.get()) transportFailure = error; }
    void networkFailure(Throwable error) { if (!finished.get()) networkFailure = error; }
    void decodingFailure(Throwable error) {
        // The outer network boundary observes application response-source transforms.
        // A new failure above it comes from OkHttp's native transparent decoder, not
        // application code. Propagated failures retain their original provenance.
        if (!causedBy(error, networkFailure)) transportFailure(error);
    }
    static boolean causedBy(Throwable error, Throwable original) {
        if (original == null) return false;
        // A fixed bound also terminates causal cycles without allocating on the
        // application's failure path. Reflection and cause access are fail-open.
        for (int depth = 0; error != null && depth < 32; depth++) {
            if (error == original) return true;
            try {
                // getCause is overridable: never execute application code to classify
                // an exception. Inherited Throwable.getCause covers standard adapters.
                if (error.getClass().getMethod("getCause").getDeclaringClass() != Throwable.class) return false;
                error = error.getCause();
            } catch (Throwable ignored) { return false; }
        }
        return false;
    }
    private boolean transportError(Throwable error) { return causedBy(error, transportFailure); }
    void closeFailure(Throwable error) { if (completeLength()) finish(null); else if (transportError(error)) finish(error); else close(); }
    void readFailure(Throwable error) { if (completeLength()) finish(null); else if (transportError(error)) finish(error); else close(); }
    void finish(Throwable error) { finish(error, false); }
    private synchronized void finish(Throwable error, boolean incomplete) {
        long lastByte = transportByteNanos == 0 ? lastByteNanos : transportByteNanos;
        long observed = error != null ? firstErrorNanos == 0 ? System.nanoTime() : firstErrorNanos : lastByte == 0 ? System.nanoTime() : lastByte;
        if (finished.get()) return;
        boolean admitted = runtime == null || runtime.startFinalizing();
        finished.set(true);
        endEpochNanos = startEpochNanos + Math.max(0, observed - start);
        try {
            if (!admitted) return;
            request.body = input.body(); request.size = input.size; request.truncated = input.truncated;
            if (input.multipart != null) { request.fields = input.multipart.fields; request.files = input.multipart.files; request.fileBytes = input.multipart.fileBytes; }
            if (!requestComplete) { request.unavailable = true; request.body = null; request.fields = null; request.files = List.of(); }
            response.error = error; response.incomplete = incomplete;
            BodyCapture capture = output;
            if (capture != null) { response.body = capture.body(); response.size = capture.size; response.truncated = capture.truncated; }
            TraceAIAuto.debug("Wire capture completed: request_complete=" + requestComplete + ", request_bytes=" + request.size + ", status=" + response.status + ", incomplete=" + incomplete);
            WireCore.Result result = WireCore.build(match, request, response, runtime == null);
            if (result == null) TraceAIAuto.debug("Wire capture skipped by protocol validation");
            if (result != null && capture != null && capture.audio != null) {
                result.attributes().put("traceai.audio.output_bytes", capture.audio.total);
                if (WireCore.contentType(response.headers).equals("text/event-stream")) {
                    result.attributes().put("gen_ai.request.stream", true);
                    if (response.firstChunkSeconds != null) result.attributes().put("gen_ai.response.time_to_first_chunk", Math.round(response.firstChunkSeconds * 1e6) / 1e6);
                }
            }
            TraceAIAuto.emit(this, result);
        } catch (Throwable ignored) { }
        finally {
            try {
                request.body = null; response.body = null; response.frames = List.of(); response.arrivals = null; request.headers = Map.of();
                input.release(); BodyCapture capture = output; if (capture != null) capture.release(); output = null; transportFailure = null; networkFailure = null;
            } finally { if (runtime != null && admitted) runtime.endFinalizing(); }
        }
    }
}
