package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import com.openai.core.RequestOptions;
import com.openai.core.http.Headers;
import com.openai.core.http.HttpClient;
import com.openai.core.http.HttpMethod;
import com.openai.core.http.HttpRequest;
import com.openai.core.http.HttpRequestBody;
import com.openai.core.http.HttpResponse;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import java.io.FilterInputStream;
import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Decorates the SDK HTTP extension point, not generated model classes. Supports synchronous and
 * asynchronous chat completions, embeddings and Responses, including SSE. Install it once as the
 * SDK client's HTTP client; keep using the SDK's own services and closeable stream responses.
 *
 * <p>Compiled against OpenAI Java 0.8.0; also usable with current SDK model packages. Does not trace
 * uploads, Realtime/WebSockets, or unrelated endpoints. Each HTTP attempt is a separate span.
 * Response bytes are observed only when the SDK/caller reads them, never drained by tracing.</p>
 */
public final class TracingHttpClient implements HttpClient {
    private final HttpClient delegate;
    private final FITracer tracer;
    private final java.util.Set<Response> responses = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private final java.util.Set<CompletableFuture<HttpResponse>> pending = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private final AtomicBoolean closed = new AtomicBoolean();

    private TracingHttpClient(HttpClient delegate, FITracer tracer) {
        this.delegate = Objects.requireNonNull(delegate, "delegate");
        this.tracer = Objects.requireNonNull(tracer, "tracer");
    }

    /** Wraps once; wrapping an already traced transport does not create duplicate spans. */
    public static HttpClient wrap(HttpClient client, FITracer tracer) {
        return client instanceof TracingHttpClient ? client : new TracingHttpClient(client, tracer);
    }

    /** Uses the configured TraceAI tracer. */
    public static HttpClient wrap(HttpClient client) { return wrap(client, TraceAI.getTracer()); }

    /**
     * Captures the calling context for a per-request SDK view. Use with the SDK's withOptions
     * method when it dispatches HTTP work on an executor without OpenTelemetry propagation.
     * This view borrows the transport: closing it does not close the shared client. Close the
     * original client at application shutdown, and do not reuse this view for another context.
     * @param transport the shared, already traced transport
     * @return a borrowed transport view bound to the current context
     */
    public static HttpClient inCurrentContext(HttpClient transport) {
        HttpClient delegate = transport instanceof ContextClient ? ((ContextClient) transport).delegate : transport;
        return new ContextClient(Objects.requireNonNull(delegate, "transport"), Context.current());
    }

    private static final class ContextClient implements HttpClient {
        private final HttpClient delegate;
        private final Context context;
        ContextClient(HttpClient delegate, Context context) { this.delegate = delegate; this.context = context; }
        @Override public HttpResponse execute(HttpRequest request) { return execute(request, RequestOptions.none()); }
        @Override public HttpResponse execute(HttpRequest request, RequestOptions options) {
            try (Scope scope = context.makeCurrent()) { return delegate.execute(request, options); }
        }
        @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest request) {
            return executeAsync(request, RequestOptions.none());
        }
        @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest request, RequestOptions options) {
            try (Scope scope = context.makeCurrent()) { return delegate.executeAsync(request, options); }
        }
        @Override public void close() { /* Borrowed: only the original client owns the shared transport. */ }
    }

    @Override public HttpResponse execute(HttpRequest request) { return execute(request, RequestOptions.none()); }

    @Override public HttpResponse execute(HttpRequest request, RequestOptions options) {
        if (closed.get()) throw new IllegalStateException("OpenAI transport is closed");
        String operation = operation(request);
        if (operation == null) return delegate.execute(request, options);
        OpenAITrace trace = new OpenAITrace(tracer, operation);
        try (Scope scope = trace.makeCurrent()) {
            return new Response(delegate.execute(capture(request, trace), options), trace);
        } catch (Throwable error) {
            trace.error(error);
            trace.end(false);
            throw OpenAITrace.propagate(error);
        }
    }

    @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest request) {
        return executeAsync(request, RequestOptions.none());
    }

    @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest request, RequestOptions options) {
        if (closed.get()) throw new IllegalStateException("OpenAI transport is closed");
        String operation = operation(request);
        if (operation == null) return delegate.executeAsync(request, options);
        OpenAITrace trace = new OpenAITrace(tracer, operation);
        final CompletableFuture<HttpResponse> upstream;
        try (Scope scope = trace.makeCurrent()) {
            upstream = delegate.executeAsync(capture(request, trace), options);
        } catch (Throwable error) {
            trace.error(error);
            trace.end(false);
            throw OpenAITrace.propagate(error);
        }
        CompletableFuture<HttpResponse> result = new CompletableFuture<>() {
            @Override public boolean cancel(boolean interrupt) {
                if (!super.cancel(interrupt)) return false;
                trace.error(new CancellationException("OpenAI request cancelled"));
                trace.end(false);
                upstream.cancel(interrupt);
                return true;
            }
        };
        pending.add(result);
        result.whenComplete((response, error) -> pending.remove(result));
        if (closed.get()) result.cancel(true);
        upstream.whenComplete((response, error) -> {
            try (Scope scope = trace.makeCurrent()) {
                if (error != null) {
                    trace.error(error instanceof CompletionException && error.getCause() != null ? error.getCause() : error);
                    trace.end(false);
                    result.completeExceptionally(error);
                } else {
                    Response wrapped = new Response(response, trace);
                    // A response arriving after cancellation still belongs to us, not a caller.
                    if (!result.complete(wrapped)) {
                        try { wrapped.close(); } catch (Exception ignored) { /* Cancel won the race. */ }
                    }
                }
            } catch (Throwable failure) {
                trace.error(failure);
                trace.end(false);
                result.completeExceptionally(failure);
            }
        });
        return result;
    }

    @Override public void close() throws Exception {
        if (!closed.compareAndSet(false, true)) return;
        Throwable failure = null;
        for (var future : pending) future.cancel(true);
        // Also release responses abandoned by generated service futures after cancellation.
        for (var response : responses) {
            try { response.close(); }
            catch (Throwable error) {
                if (failure == null) failure = error; else if (error != failure) failure.addSuppressed(error);
            }
        }
        try { delegate.close(); }
        catch (Throwable error) {
            if (failure == null) failure = error; else if (error != failure) failure.addSuppressed(error);
        }
        if (failure != null) throw OpenAITrace.propagate(failure);
    }

    private static String operation(HttpRequest request) {
        if (Boolean.TRUE.equals(Context.current().get(OpenAITrace.LEGACY)) || request.method() != HttpMethod.POST) return null;
        List<String> path = request.pathSegments();
        String joined = String.join("/", path);
        if (joined.equals("chat/completions")) return "chat";
        if (joined.equals("embeddings")) return "embeddings";
        if (joined.equals("responses")) return "responses";
        return null;
    }

    private static HttpRequest capture(HttpRequest request, OpenAITrace trace) {
        HttpRequestBody body = request.body();
        if (body == null || body.contentType() == null || !body.contentType().startsWith("application/json")) return request;
        return request.toBuilder().body(new HttpRequestBody() {
            @Override public void writeTo(OutputStream out) throws IOException {
                OpenAITrace.Capture captured = new OpenAITrace.Capture();
                try (Scope scope = trace.makeCurrent()) {
                    body.writeTo(new FilterOutputStream(out) {
                        @Override public void write(int b) throws IOException { out.write(b); captured.write(b); }
                        @Override public void write(byte[] b, int off, int len) throws IOException {
                            out.write(b, off, len);
                            captured.write(b, off, len);
                        }
                    });
                    byte[] bytes = captured.completeBytes();
                    if (bytes != null) trace.request(bytes);
                }
            }
            @Override public String contentType() { return body.contentType(); }
            @Override public long contentLength() { return body.contentLength(); }
            @Override public boolean repeatable() { return body.repeatable(); }
            @Override public void close() { body.close(); }
        }).build();
    }

    private final class Response implements HttpResponse {
        private final HttpResponse delegate;
        private final OpenAITrace trace;
        private final AtomicBoolean closed = new AtomicBoolean();
        private InputStream body;

        Response(HttpResponse delegate, OpenAITrace trace) {
            this.delegate = delegate;
            this.trace = trace;
            try {
                trace.status(delegate.statusCode());
                responses.add(this);
                if (TracingHttpClient.this.closed.get()) {
                    throw new CancellationException("OpenAI transport closed during request");
                }
            } catch (Throwable error) {
                trace.error(error);
                try { close(); } catch (Throwable closing) { if (closing != error) error.addSuppressed(closing); }
                throw OpenAITrace.propagate(error);
            }
        }
        @Override public int statusCode() { return delegate.statusCode(); }
        @Override public Headers headers() { return delegate.headers(); }

        @Override public synchronized InputStream body() {
            if (body != null) return body;
            try (Scope scope = trace.makeCurrent()) {
                boolean streaming = headers().values("content-type").stream()
                    .anyMatch(value -> value.toLowerCase(java.util.Locale.ROOT).startsWith("text/event-stream"));
                body = new FilterInputStream(delegate.body()) {
                    private final OpenAITrace.Capture captured = new OpenAITrace.Capture();
                    private final OpenAITrace.Capture line = new OpenAITrace.Capture();
                    private final StringBuilder event = new StringBuilder();
                    private boolean eventOverflow;
                    private boolean exhausted;

                    @Override public int read() throws IOException {
                        byte[] one = new byte[1];
                        return read(one, 0, 1) < 0 ? -1 : one[0] & 255;
                    }
                    @Override public int read(byte[] b, int off, int len) throws IOException {
                        try (Scope scope = trace.makeCurrent()) {
                            int count = in.read(b, off, len);
                            if (count < 0 && !exhausted) {
                                exhausted = true;
                                if (!streaming) {
                                    byte[] bytes = captured.completeBytes();
                                    if (bytes != null) trace.response(bytes);
                                }
                                // EOF without the SSE completion marker is not a successful generation.
                                trace.end(!streaming);
                            } else if (count > 0) {
                                if (streaming) {
                                    for (int i = off; i < off + count; i++) accept(b[i]);
                                } else captured.write(b, off, count);
                            }
                            return count;
                        } catch (IOException | RuntimeException | Error error) {
                            trace.error(error);
                            trace.end(false);
                            throw OpenAITrace.propagate(error);
                        }
                    }
                    private void accept(byte value) {
                        if (value != '\n') { line.write(value); return; }
                        byte[] bytes = line.completeBytes();
                        line.reset();
                        if (bytes == null) { eventOverflow = true; return; }
                        String text = new String(bytes, StandardCharsets.UTF_8);
                        if (text.endsWith("\r")) text = text.substring(0, text.length() - 1);
                        if (text.isEmpty()) {
                            if (!eventOverflow && event.length() > 0) trace.event(event.toString());
                            event.setLength(0);
                            eventOverflow = false;
                        } else if (text.startsWith("data:")) {
                            String data = text.substring(5);
                            if (data.startsWith(" ")) data = data.substring(1);
                            if (event.length() + data.length() + 1 > OpenAITrace.LIMIT) eventOverflow = true;
                            if (!eventOverflow) {
                                if (event.length() > 0) event.append('\n');
                                event.append(data);
                            }
                        }
                    }
                    // Observing skipped bytes keeps the parser consistent without changing skip's bounds.
                    @Override public long skip(long n) throws IOException {
                        byte[] buffer = new byte[(int) Math.min(8192, Math.max(0, n))];
                        long skipped = 0;
                        while (skipped < n) {
                            int read = read(buffer, 0, (int) Math.min(buffer.length, n - skipped));
                            if (read <= 0) break;
                            skipped += read;
                        }
                        return skipped;
                    }
                    @Override public void close() throws IOException {
                        try { Response.this.close(); }
                        catch (IOException error) { throw OpenAITrace.propagate(error); }
                        catch (RuntimeException error) { throw OpenAITrace.propagate(error); }
                        catch (Exception error) { throw new IOException(error); }
                    }
                };
                return body;
            } catch (Throwable error) {
                trace.error(error);
                trace.end(false);
                throw OpenAITrace.propagate(error);
            }
        }

        @Override public void close() throws Exception {
            if (!closed.compareAndSet(false, true)) return;
            try (Scope scope = trace.makeCurrent()) {
                delegate.close();
            } catch (Exception | Error error) {
                trace.error(error);
                throw OpenAITrace.propagate(error);
            } finally {
                responses.remove(this);
                trace.end(false);
            }
        }
    }
}
