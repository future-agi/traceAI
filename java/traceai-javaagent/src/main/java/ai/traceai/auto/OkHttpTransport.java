package ai.traceai.auto;

import okhttp3.*;
import okio.*;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.*;

/** Loaded only after an application supplies OkHttp. OkHttp/Okio are not bundled in the agent. */
final class OkHttpTransport implements Interceptor {
    private static final Map<Call, OtelBridge.Snapshot> CONTEXTS = Collections.synchronizedMap(new WeakHashMap<>());
    private static final Map<Call, WireCapture> CAPTURES = Collections.synchronizedMap(new WeakHashMap<>());
    static final class CredentialsObserver implements Interceptor {
        @Override public Response intercept(Chain chain) throws IOException {
            try { observeRequest(chain.request()); } catch (Throwable ignored) { }
            WireCapture capture = CAPTURES.get(chain.call());
            Response response = chain.proceed(chain.request());
            if (capture != null) {
                capture.responseHeaders(response.code(), headers(response.headers()));
                OkHttpSources.body(response.body(), capture, OkHttpSources.TRANSPORT);
            }
            return response;
        }
    }
    static final class NetworkBoundary implements Interceptor {
        @Override public Response intercept(Chain chain) throws IOException {
            Response response = chain.proceed(chain.request());
            OkHttpSources.body(response.body(), CAPTURES.get(chain.call()), OkHttpSources.NETWORK);
            return response;
        }
    }
    static final class DecodingObserver implements Interceptor {
        @Override public Response intercept(Chain chain) throws IOException {
            Response response = chain.proceed(chain.request());
            OkHttpSources.body(response.body(), CAPTURES.get(chain.call()), OkHttpSources.DECODED);
            return response;
        }
    }
    static void requestContext(Object raw) {
        Call call = (Call)raw;
        // Timestamp before matching, credential copying, or any agent validation.
        OtelBridge.Snapshot context = OtelBridge.snapshot();
        observeRequest(call.request());
        if (TraceAIAuto.transportEnabled("okhttp") && WireCore.match(call.request().method(), call.request().url().toString()) != null) CONTEXTS.put(call, context);
    }
    static void observeRequest(Object raw) { Request request = (Request)raw; KnownSecrets.observe(headers(request.headers()), request.url().toString()); }
    static boolean compatible() {
        try {
            Interceptor.Chain.class.getMethod("call");
            RequestBody.class.getMethod("isOneShot"); RequestBody.class.getMethod("isDuplex");
            return true;
        } catch (ReflectiveOperationException ignored) { return false; }
    }
    static void install(Object raw) {
        if (!compatible()) { TraceAIAuto.integrationFailed("okhttp"); return; }
        OkHttpClient.Builder builder = (OkHttpClient.Builder)raw;
        if (builder.interceptors().stream().noneMatch(OkHttpTransport.class::isInstance)) builder.addInterceptor(new OkHttpTransport());
        builder.interceptors().removeIf(DecodingObserver.class::isInstance);
        builder.addInterceptor(new DecodingObserver());
        builder.networkInterceptors().removeIf(i -> i instanceof CredentialsObserver || i instanceof NetworkBoundary);
        builder.networkInterceptors().add(0, new NetworkBoundary());
        builder.addNetworkInterceptor(new CredentialsObserver());
    }
    static Map<String, String> headers(Headers raw) {
        Map<String, String> headers = new LinkedHashMap<>(); for (String name : raw.names()) headers.put(name, String.join("\n", raw.values(name))); return headers;
    }
    @Override public Response intercept(Chain chain) throws IOException {
        Request request = chain.request();
        OtelBridge.Snapshot context = CONTEXTS.remove(chain.call());
        if (context == null) context = OtelBridge.snapshot();
        Map<String, String> requestHeaders = headers(request.headers());
        WireCapture capture = TraceAIAuto.begin(request.method(), request.url().toString(), requestHeaders, context);
        if (capture == null) return chain.proceed(request);
        Request wrapped = request;
        try {
            RequestBody body = request.body();
            if (body == null) capture.requestComplete();
            final MediaType contentType = body == null ? null : body.contentType();
            if (contentType != null) {
                capture.request.headers.putIfAbsent("content-type", contentType.toString());
                capture.input.release();
                capture.input = new BodyCapture(WireCore.rawHeader(capture.request.headers, "content-type"), capture.request.limit, false);
            }
            if (body != null) wrapped = request.newBuilder().method(request.method(), new RequestBody() {
                @Override public MediaType contentType() { return contentType; }
                @Override public long contentLength() throws IOException { return body.contentLength(); }
                @Override public boolean isDuplex() { return body.isDuplex(); }
                @Override public boolean isOneShot() { return body.isOneShot(); }
                @Override public void writeTo(BufferedSink sink) throws IOException {
                    capture.requestStarted();
                    BufferedSink tee = Okio.buffer(new ForwardingSink(sink) {
                        @Override public void write(Buffer source, long count) throws IOException {
                            try { copy(source, 0, count, capture, true); } catch (Throwable ignored) { }
                            super.write(source, count);
                        }
                    });
                    body.writeTo(tee);
                    if (tee.isOpen()) tee.emit();
                    capture.requestComplete();
                }
            }).build();
        } catch (Throwable ignored) { wrapped = request; }
        TraceAIAuto.IN_FLIGHT.set(true);
        Response response;
        CAPTURES.put(chain.call(), capture);
        try { response = chain.proceed(wrapped); }
        catch (IOException | RuntimeException | Error error) {
            if (capture.response.status == null) capture.finish(error); else capture.readFailure(error);
            throw error;
        }
        finally { CAPTURES.remove(chain.call()); TraceAIAuto.IN_FLIGHT.remove(); }
        try {
            for (Response attempt = response; attempt != null; attempt = attempt.priorResponse()) observeRequest(attempt.request());
            capture.responseHeaders(response.code(), headers(response.headers()));
            capture.transportReturned = true;
            if (response.body() == null) capture.finish(null);
            else OkHttpSources.body(response.body(), capture, OkHttpSources.OUTPUT);
        } catch (Throwable ignored) { capture.close(); }
        return response;
    }
    static void copy(Buffer source, long offset, long count, WireCapture capture, boolean input) { copy(source, offset, count, capture, input, System.nanoTime()); }
    static void copy(Buffer source, long offset, long count, WireCapture capture, boolean input, long observed) {
        if (input && capture.input.binary) { capture.input.size += count; return; }
        if (!input && WireCore.binary(WireCore.contentType(capture.response.headers))) { capture.responseCount(count, observed); return; }
        long remaining = count;
        while (remaining > 0) {
            int n = (int)Math.min(8192, remaining);
            Buffer peek = new Buffer(); source.copyTo(peek, offset, n);
            ByteBuffer bytes = ByteBuffer.wrap(peek.readByteArray());
            if (input) capture.requestBytes(bytes); else capture.responseBytes(bytes, observed);
            offset += n; remaining -= n;
        }
    }
}
