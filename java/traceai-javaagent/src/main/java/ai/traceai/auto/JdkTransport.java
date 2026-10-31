package ai.traceai.auto;

import java.net.URI;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.Flow;
import javax.net.ssl.SSLSession;

final class JdkTransport {
    private static final ThreadLocal<WireCapture> BODY_OWNER = new ThreadLocal<>();
    private static final Map<Object, WireCapture> STREAMS = Collections.synchronizedMap(new WeakHashMap<>());
    static void body(Object stream) { WireCapture owner = BODY_OWNER.get(); if (owner != null) STREAMS.put(stream, owner); TraceAIAuto.debug("JDK stream body registered: " + (owner != null)); }
    static void closed(Object stream) { WireCapture owner = STREAMS.remove(stream); TraceAIAuto.debug("JDK stream closed: " + (owner != null)); if (owner != null) owner.close(); }
    static void header(Object raw, String name, String value) {
        if (name != null && value != null) KnownSecrets.observe(Map.of(name, value), raw instanceof HttpRequest request ? request.uri().toString() : null);
    }
    static Map<String, String> headers(HttpHeaders headers) {
        Map<String, String> result = new LinkedHashMap<>(); headers.map().forEach((k, v) -> result.put(k, String.join("\n", v))); return result;
    }
    static Object[] enter(Object rawRequest, Object rawHandler) {
        try {
            OtelBridge.Snapshot context = OtelBridge.snapshot();
            HttpRequest request = (HttpRequest)rawRequest;
            Map<String, String> requestHeaders = headers(request.headers()); String url = request.uri().toString();
            if (TraceAIAuto.currentRuntime() != null) KnownSecrets.observe(requestHeaders, url);
            if (!TraceAIAuto.transportEnabled("java.net.http")) return null;
            WireCapture capture = TraceAIAuto.begin(request.method(), url, requestHeaders, context);
            if (capture == null) return null;
            if (request.bodyPublisher().isEmpty()) capture.requestComplete();
            HttpRequest wrapped = new CapturedRequest(request, capture);
            @SuppressWarnings("unchecked") HttpResponse.BodyHandler<Object> handler = (HttpResponse.BodyHandler<Object>)rawHandler;
            HttpResponse.BodyHandler<Object> tee = info -> {
                capture.responseHeaders(info.statusCode(), headers(info.headers()));
                try { return subscriber(handler.apply(info), capture); }
                catch (Throwable error) { capture.close(); throw error; }
            };
            TraceAIAuto.IN_FLIGHT.set(true);
            return new Object[]{wrapped, tee, capture};
        } catch (Throwable ignored) { return null; }
    }
    static void exit(Object state, Object returned, Throwable error) {
        try {
            TraceAIAuto.IN_FLIGHT.remove();
            WireCapture capture = (WireCapture)((Object[])state)[2];
            if (error != null) failed(capture, error);
            else if (returned instanceof CompletionStage<?> stage) stage.whenComplete((response, failure) -> { if (failure != null) failed(capture, unwrap(failure)); });
        } catch (Throwable ignored) { }
    }
    private static void failed(WireCapture capture, Throwable error) {
        // Body transport failures arrive through onError first. A failed application body stage
        // can fail send/sendAsync without that signal; it is a consumer abort, not a reset.
        if (capture.response.status != null) capture.close(); else capture.finish(error);
    }
    static Throwable unwrap(Throwable error) { return error instanceof CompletionException && error.getCause() != null ? error.getCause() : error; }
    static <T> HttpResponse.BodySubscriber<T> subscriber(HttpResponse.BodySubscriber<T> delegate, WireCapture capture) {
        return new HttpResponse.BodySubscriber<>() {
            @Override public CompletionStage<T> getBody() {
                WireCapture previous = BODY_OWNER.get(); BODY_OWNER.set(capture);
                try { return delegate.getBody(); }
                catch (Throwable error) { capture.close(); throw error; }
                finally { if (previous == null) BODY_OWNER.remove(); else BODY_OWNER.set(previous); }
            }
            @Override public void onSubscribe(Flow.Subscription subscription) {
                try {
                    delegate.onSubscribe(new Flow.Subscription() {
                        @Override public void request(long n) { subscription.request(n); }
                        @Override public void cancel() { capture.close(); subscription.cancel(); }
                    });
                } catch (Throwable error) { capture.close(); throw error; }
            }
            @Override public void onNext(List<ByteBuffer> buffers) {
                long observed = System.nanoTime();
                for (ByteBuffer buffer : buffers) capture.responseBytes(buffer, observed);
                try { delegate.onNext(buffers); } catch (Throwable error) { capture.close(); throw error; }
            }
            @Override public void onError(Throwable error) { capture.errorObserved(System.nanoTime()); capture.finish(error); delegate.onError(error); }
            @Override public void onComplete() { capture.finish(null); delegate.onComplete(); }
        };
    }
    static final class CapturedRequest extends HttpRequest {
        final HttpRequest delegate; final WireCapture capture;
        CapturedRequest(HttpRequest delegate, WireCapture capture) { this.delegate = delegate; this.capture = capture; }
        @Override public Optional<BodyPublisher> bodyPublisher() {
            return delegate.bodyPublisher().map(publisher -> new BodyPublisher() {
                @Override public long contentLength() { return publisher.contentLength(); }
                @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> downstream) {
                    // Each transport subscription is a replay, not another fragment of the logical body.
                    capture.requestStarted();
                    // Never subscribe eagerly: backpressure, cancellation, and one-shot publishers belong to the app.
                    publisher.subscribe(new Flow.Subscriber<>() {
                        @Override public void onSubscribe(Flow.Subscription subscription) { downstream.onSubscribe(subscription); }
                        @Override public void onNext(ByteBuffer buffer) { capture.requestBytes(buffer); downstream.onNext(buffer); }
                        @Override public void onError(Throwable error) { capture.finish(error); downstream.onError(error); }
                        @Override public void onComplete() { capture.requestComplete(); downstream.onComplete(); }
                    });
                }
            });
        }
        @Override public String method() { return delegate.method(); }
        @Override public Optional<Duration> timeout() { return delegate.timeout(); }
        @Override public boolean expectContinue() { return delegate.expectContinue(); }
        @Override public URI uri() { return delegate.uri(); }
        @Override public Optional<HttpClient.Version> version() { return delegate.version(); }
        @Override public HttpHeaders headers() { return delegate.headers(); }
    }
    private JdkTransport() {}
}
