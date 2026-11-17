package ai.traceai.auto;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.*;
import okhttp3.*;
import okio.*;
import java.io.*;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import static org.junit.jupiter.api.Assertions.*;

class TransportSafetyTest {
    @BeforeAll static void observeOriginalSources() { TraceAIAgent.install(net.bytebuddy.agent.ByteBuddyAgent.install()); }
    InMemorySpanExporter exporter;
    SdkTracerProvider provider;
    Object oldConfig, oldActive;
    TraceAIAuto.RuntimeState oldRuntime;
    AtomicReference<TraceAIAuto.RuntimeState> runtime;
    static Field field(String name) throws Exception { Field field = TraceAIAuto.class.getDeclaredField(name); field.setAccessible(true); return field; }
    @SuppressWarnings("unchecked") @BeforeEach void initializeOwnedTestRuntime() throws Exception {
        exporter = InMemorySpanExporter.create(); provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        AutoConfig config = AutoConfig.resolve(Map.of("api_key", "FI_SECRET_123", "secret_key", "FI_OTHER_123"), Map.of());
        oldConfig = field("config").get(null); oldActive = field("active").get(null);
        runtime = (AtomicReference<TraceAIAuto.RuntimeState>)field("RUNTIME").get(null);
        oldRuntime = runtime.getAndSet(new TraceAIAuto.RuntimeState(config, provider, provider.get("test"), new OtlpJsonExporter(config)));
        field("config").set(null, config); field("active").set(null, true);
    }
    @AfterEach void restoreRuntime() throws Exception {
        runtime.set(oldRuntime); field("config").set(null, oldConfig); field("active").set(null, oldActive); TraceAIAuto.IN_FLIGHT.remove(); provider.shutdown();
    }
    static String chunk(String text) { return "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"" + text + "\"}}]}\n\n"; }
    static final class TestSource implements Source {
        final Buffer bytes = new Buffer().writeUtf8(chunk("partial"));
        boolean failRead, failClose, closed;
        Exception applicationFailure;
        public long read(Buffer sink, long count) throws IOException {
            if (count == 0) return 0;
            if (bytes.size() > 0) return bytes.read(sink, Math.min(count, bytes.size()));
            if (applicationFailure instanceof IOException error) throw error;
            if (applicationFailure instanceof RuntimeException error) throw error;
            if (failRead) throw new IOException("reset echoed REQUEST_SECRET"); return -1;
        }
        public okio.Timeout timeout() { return okio.Timeout.NONE; }
        public void close() throws IOException { closed = true; if (failClose) throw new IOException("close failure"); }
    }
    static class TestChain implements Interceptor.Chain {
        final Request request = new Request.Builder().url("https://api.openai.com/v1/chat/completions").header("Authorization", "Token REQUEST_SECRET").post(RequestBody.create(MediaType.get("application/json"), PlainApp.REQUEST)).build();
        final Call call = new OkHttpClient().newCall(request);
        final TestSource source;
        boolean failConnect, replay, rejectUpload;
        String written;
        long length = -1;
        TestChain(TestSource source) { this.source = source; }
        public Request request() { return request; }
        public Response proceed(Request request) throws IOException {
            if (failConnect) throw new java.net.ConnectException("connection refused");
            if (!rejectUpload) {
                Buffer sink = new Buffer(); request.body().writeTo(sink); written = sink.readUtf8();
                if (replay) { sink = new Buffer(); request.body().writeTo(sink); assertEquals(written, sink.readUtf8()); }
            }
            var builder = new Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(rejectUpload ? 401 : 200).message("OK").header("content-type", "text/event-stream");
            if (length >= 0) builder.header("Content-Length", String.valueOf(length));
            Response raw = builder.body(ResponseBody.create(MediaType.get("text/event-stream"), length, Okio.buffer(source))).build();
            if (source.applicationFailure != null) return raw; // application source substituted above the native boundary
            Interceptor.Chain network = (Interceptor.Chain)java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Interceptor.Chain.class},
                (proxy, method, args) -> method.getName().equals("proceed") ? raw : method.invoke(this, args));
            return new OkHttpTransport.CredentialsObserver().intercept(network);
        }
        public Connection connection() { return null; }
        public Call call() { return call; }
        public int connectTimeoutMillis() { return 100; }
        public Interceptor.Chain withConnectTimeout(int timeout, TimeUnit unit) { return this; }
        public int readTimeoutMillis() { return 100; }
        public Interceptor.Chain withReadTimeout(int timeout, TimeUnit unit) { return this; }
        public int writeTimeoutMillis() { return 100; }
        public Interceptor.Chain withWriteTimeout(int timeout, TimeUnit unit) { return this; }
    }
    @Test void okhttpTransportErrorKeepsPartialOutputAndOriginalException() throws Exception {
        TestSource source = new TestSource(); source.failRead = true; TestChain chain = new TestChain(source);
        Response response = new OkHttpTransport().intercept(chain); Buffer received = new Buffer();
        assertTrue(response.body().source().read(received, 8192) > 0);
        IOException error = assertThrows(IOException.class, () -> response.body().source().read(received, 8192));
        assertEquals("reset echoed REQUEST_SECRET", error.getMessage()); assertEquals(PlainApp.REQUEST, chain.written);
        assertEquals(1, exporter.getFinishedSpanItems().size()); var span = exporter.getFinishedSpanItems().get(0);
        assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
        assertEquals("reset echoed REQUEST_SECRET", span.getStatus().getDescription(), "privacy is deferred to export, not transport completion");
        String exported = WireCore.dump(new OtlpJsonExporter(TraceAIAuto.config()).serialize(span));
        assertFalse(exported.contains("REQUEST_SECRET")); assertTrue(exported.contains("reset echoed <redacted>"));
        assertEquals("partial", span.getAttributes().get(AttributeKey.stringKey("output.value"))); assertEquals("IOException", span.getAttributes().get(AttributeKey.stringKey("error.type")));
        response.close(); assertEquals(1, exporter.getFinishedSpanItems().size());
    }
    @Test void wrappedNativeReadFailuresRemainErrorsButIndependentAbortsDoNot() throws Exception {
        for (boolean propagateCause : List.of(true, false)) {
            TestSource source = new TestSource(); source.failRead = true;
            var thrown = new AtomicReference<IOException>(); var nativeThrown = new AtomicReference<IOException>();
            TestChain chain = new TestChain(source) {
                @Override public Response proceed(Request request) throws IOException {
                    Response response = super.proceed(request); ResponseBody body = response.body();
                    return response.newBuilder().body(new ResponseBody() {
                        final BufferedSource adapted = Okio.buffer(new ForwardingSource(body.source()) {
                            @Override public long read(Buffer sink, long count) throws IOException {
                                try { return super.read(sink, count); }
                                catch (IOException original) {
                                    nativeThrown.set(original);
                                    IOException error = propagateCause ? new IOException("app adapter wrapping native failure", new IOException("inner adapter", original)) : new IOException("independent application abort");
                                    thrown.set(error); throw error;
                                }
                            }
                        });
                        public MediaType contentType() { return body.contentType(); }
                        public long contentLength() { return body.contentLength(); }
                        public BufferedSource source() { return adapted; }
                    }).build();
                }
            };
            try (var response = new OkHttpTransport().intercept(chain)) {
                var received = new Buffer(); assertTrue(response.body().source().read(received, 8192) > 0);
                var error = assertThrows(IOException.class, () -> response.body().source().read(received, 8192));
                assertSame(thrown.get(), error); assertNotNull(nativeThrown.get());
                var span = exporter.getFinishedSpanItems().get(exporter.getFinishedSpanItems().size() - 1);
                assertEquals(propagateCause ? StatusCode.ERROR : StatusCode.OK, span.getStatus().getStatusCode());
                assertEquals(propagateCause ? null : true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
                assertEquals("partial", span.getAttributes().get(AttributeKey.stringKey("output.value")));
            }
        }
        assertEquals(2, exporter.getFinishedSpanItems().size());
    }
    @Test void okhttpZeroLengthReadAndEarlyCloseFinalizeExactlyOnce() throws Exception {
        TestSource source = new TestSource(); TestChain chain = new TestChain(source); Response response = new OkHttpTransport().intercept(chain);
        assertEquals(0, response.body().source().read(new Buffer(), 0)); assertTrue(exporter.getFinishedSpanItems().isEmpty());
        response.close(); response.close(); assertTrue(source.closed); assertEquals(1, exporter.getFinishedSpanItems().size());
        var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.OK, span.getStatus().getStatusCode()); assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
    }
    @Test void okhttpCloseFailureIsNotReportedAsConsumerCancellation() throws Exception {
        TestSource source = new TestSource(); source.failClose = true; Response response = new OkHttpTransport().intercept(new TestChain(source));
        assertThrows(IOException.class, () -> response.body().source().close()); var span = exporter.getFinishedSpanItems().get(0);
        assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode()); assertNull(span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
    }
    @Test void wrappedNativeCloseFailureRetainsErrorProvenance() {
        var capture = streamingCapture(Map.of("content-type", "text/event-stream"));
        capture.responseBytes(java.nio.ByteBuffer.wrap(chunk("partial").getBytes(StandardCharsets.UTF_8)));
        var nativeError = new IOException("native close failure"); capture.transportFailure(nativeError);
        capture.closeFailure(new IOException("app close adapter", nativeError)); capture.close();
        assertEquals(1, exporter.getFinishedSpanItems().size()); var span = exporter.getFinishedSpanItems().get(0);
        assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
        assertNull(span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
    }
    @Test void eagerNetworkInterceptorAbortKeepsHttpMetadataAndOriginalException() throws Exception {
        var aborted = new IOException("application parser deliberately aborted");
        var chain = new TestChain(new TestSource()) {
            @Override public Response proceed(Request request) throws IOException {
                Response response = super.proceed(request);
                response.body().source().read(new Buffer(), 1);
                throw aborted;
            }
        };
        assertSame(aborted, assertThrows(IOException.class, () -> new OkHttpTransport().intercept(chain)));
        assertEquals(1, exporter.getFinishedSpanItems().size()); var span = exporter.getFinishedSpanItems().get(0);
        assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
        assertEquals(200L, span.getAttributes().get(AttributeKey.longKey("http.response.status_code")));
        assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
        assertNull(span.getAttributes().get(AttributeKey.stringKey("error.type")));
    }
    @Test void spanEndUsesObservedByteTimestampRatherThanCompletionOrParsingTime() throws Exception {
        var capture = streamingCapture(Map.of("content-type", "text/event-stream"));
        capture.responseBytes(java.nio.ByteBuffer.wrap(chunk("partial").getBytes(StandardCharsets.UTF_8)));
        long end = capture.startEpochNanos + capture.lastByteNanos - capture.start;
        Thread.sleep(30); capture.finish(null);
        assertEquals(end, exporter.getFinishedSpanItems().get(0).getEndEpochNanos());
    }
    @Test void okhttpConnectionFailureBeforeSendingStillProducesSpan() throws Exception {
        TestChain chain = new TestChain(new TestSource()); chain.failConnect = true;
        assertThrows(java.net.ConnectException.class, () -> new OkHttpTransport().intercept(chain));
        var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
        assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.body_unavailable"))); assertNull(span.getAttributes().get(AttributeKey.stringKey("input.value")));
    }
    @Test void jdkStreamCloseBeforeOnSubscribeFinalizesWithoutReplacingStream() throws Exception {
        var capture = streamingCapture(Map.of("content-type", "application/json"));
        var delegate = java.net.http.HttpResponse.BodySubscribers.ofInputStream();
        var tee = JdkTransport.subscriber(delegate, capture);
        var original = delegate.getBody().toCompletableFuture().join();
        var observed = tee.getBody().toCompletableFuture().join();
        assertSame(original, observed); observed.close();
        assertEquals(1, exporter.getFinishedSpanItems().size());
        assertEquals(true, exporter.getFinishedSpanItems().get(0).getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
    }
    @Test void jdkSubscriberFailureRetainsPartialOutput() {
        WireCapture capture = TraceAIAuto.begin("POST", "https://api.openai.com/v1/chat/completions", Map.of("content-type", "application/json")); assertNotNull(capture);
        capture.requestBytes(java.nio.ByteBuffer.wrap(PlainApp.REQUEST.getBytes(StandardCharsets.UTF_8))); capture.requestComplete(); capture.responseHeaders(200, Map.of("content-type", "text/event-stream"));
        var subscriber = JdkTransport.subscriber(java.net.http.HttpResponse.BodySubscribers.ofByteArray(), capture);
        subscriber.onSubscribe(new java.util.concurrent.Flow.Subscription() { public void request(long count) {} public void cancel() {} });
        subscriber.onNext(List.of(java.nio.ByteBuffer.wrap(chunk("partial").getBytes(StandardCharsets.UTF_8))));
        var error = new java.net.SocketTimeoutException("timeout"); subscriber.onError(error);
        CompletionException actual = assertThrows(CompletionException.class, () -> subscriber.getBody().toCompletableFuture().join()); assertSame(error, actual.getCause());
        var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode()); assertEquals("partial", span.getAttributes().get(AttributeKey.stringKey("output.value")));
    }
    @Test void okhttpReplayCapturesOneLogicalRequestInsteadOfConcatenatedJson() throws Exception {
        TestChain chain = new TestChain(new TestSource()); chain.replay = true;
        try (Response response = new OkHttpTransport().intercept(chain)) { response.body().string(); }
        assertEquals(1, exporter.getFinishedSpanItems().size());
        assertEquals(PlainApp.REQUEST, exporter.getFinishedSpanItems().get(0).getAttributes().get(AttributeKey.stringKey("input.value")));
    }
    @Test void jdkReplayResetsCaptureOnEveryPublisherSubscription() {
        var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create("https://api.openai.com/v1/chat/completions")).header("content-type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
        Object[] state = JdkTransport.enter(request, java.net.http.HttpResponse.BodyHandlers.ofString()); assertNotNull(state);
        try {
            var wrapped = (java.net.http.HttpRequest)state[0];
            for (int i = 0; i < 2; i++) wrapped.bodyPublisher().orElseThrow().subscribe(new java.util.concurrent.Flow.Subscriber<>() {
                public void onSubscribe(java.util.concurrent.Flow.Subscription subscription) { subscription.request(Long.MAX_VALUE); }
                public void onNext(java.nio.ByteBuffer buffer) { }
                public void onError(Throwable error) { fail(error); }
                public void onComplete() { }
            });
            WireCapture capture = (WireCapture)state[2]; capture.responseHeaders(200, Map.of("content-type", "application/json")); capture.finish(null);
            assertEquals(1, exporter.getFinishedSpanItems().size());
            assertEquals(PlainApp.REQUEST, exporter.getFinishedSpanItems().get(0).getAttributes().get(AttributeKey.stringKey("input.value")));
        } finally { JdkTransport.exit(state, null, null); }
    }
    @Test void okhttpEarlyFinalResponseWithoutUploadIsBodyUnavailable() throws Exception {
        TestChain chain = new TestChain(new TestSource()); chain.rejectUpload = true;
        try (Response response = new OkHttpTransport().intercept(chain)) { assertEquals(401, response.code()); response.body().string(); }
        assertUnavailableHttpFailure();
    }
    @Test void jdkEarlyFinalResponseWithoutUploadIsBodyUnavailable() {
        var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create("https://api.openai.com/v1/chat/completions")).expectContinue(true).POST(java.net.http.HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
        Object[] state = JdkTransport.enter(request, java.net.http.HttpResponse.BodyHandlers.ofString()); assertNotNull(state);
        try {
            WireCapture capture = (WireCapture)state[2]; capture.responseHeaders(401, Map.of()); capture.finish(null); assertUnavailableHttpFailure();
        } finally { JdkTransport.exit(state, null, null); }
    }
    void assertUnavailableHttpFailure() {
        assertEquals(1, exporter.getFinishedSpanItems().size()); var span = exporter.getFinishedSpanItems().get(0);
        assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode()); assertEquals("401", span.getAttributes().get(AttributeKey.stringKey("error.type")));
        assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.body_unavailable"))); assertNull(span.getAttributes().get(AttributeKey.stringKey("input.value")));
    }
    WireCapture streamingCapture(Map<String, String> responseHeaders) {
        WireCapture capture = TraceAIAuto.begin("POST", "https://api.openai.com/v1/chat/completions", Map.of("content-type", "application/json"));
        assertNotNull(capture); capture.requestBytes(java.nio.ByteBuffer.wrap(PlainApp.REQUEST.getBytes(StandardCharsets.UTF_8))); capture.requestComplete();
        capture.responseHeaders(200, responseHeaders); return capture;
    }
    @Test void applicationSubscriberExceptionIsConsumerAbortAndOriginalThrowableSurvives() {
        var capture = streamingCapture(Map.of("content-type", "text/event-stream"));
        var error = new IllegalStateException("application consumer failure");
        var subscriber = JdkTransport.subscriber(new java.net.http.HttpResponse.BodySubscriber<Void>() {
            public CompletionStage<Void> getBody() { return CompletableFuture.completedFuture(null); }
            public void onSubscribe(java.util.concurrent.Flow.Subscription subscription) { }
            public void onNext(List<java.nio.ByteBuffer> buffers) { throw error; }
            public void onError(Throwable failure) { }
            public void onComplete() { }
        }, capture);
        assertSame(error, assertThrows(IllegalStateException.class, () -> subscriber.onNext(List.of(java.nio.ByteBuffer.wrap(chunk("partial").getBytes(StandardCharsets.UTF_8))))));
        capture.finish(new IOException("late transport cancellation"));
        var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
        assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete"))); assertNull(span.getAttributes().get(AttributeKey.stringKey("error.type")));
        assertEquals("partial", span.getAttributes().get(AttributeKey.stringKey("output.value"))); assertEquals(1, exporter.getFinishedSpanItems().size());
    }
    @Test void applicationBodyFutureFailureIsConsumerAbortWithoutReplacingFuture() {
        var capture = streamingCapture(Map.of("content-type", "text/event-stream"));
        var body = new CompletableFuture<Void>(); var error = new IOException("application consumer failure");
        var subscriber = JdkTransport.subscriber(new java.net.http.HttpResponse.BodySubscriber<Void>() {
            public CompletionStage<Void> getBody() { return body; }
            public void onSubscribe(java.util.concurrent.Flow.Subscription subscription) { }
            public void onNext(List<java.nio.ByteBuffer> buffers) { }
            public void onError(Throwable failure) { }
            public void onComplete() { }
        }, capture);
        assertSame(body, subscriber.getBody());
        var clientResult = new CompletableFuture<Void>();
        JdkTransport.exit(new Object[]{null, null, capture}, clientResult, null);
        body.completeExceptionally(error); clientResult.completeExceptionally(error); capture.finish(error);
        assertSame(error, assertThrows(CompletionException.class, body::join).getCause());
        var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
        assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
    }
    @Test void jdkHandlerExceptionsAreConsumerAbortsNotTransportFailures() {
        var error = new IllegalArgumentException("application handler");
        var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create("https://api.openai.com/v1/chat/completions")).POST(java.net.http.HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
        Object[] state = JdkTransport.enter(request, (java.net.http.HttpResponse.BodyHandler<Void>)info -> { throw error; });
        assertNotNull(state);
        try {
            @SuppressWarnings("unchecked") var handler = (java.net.http.HttpResponse.BodyHandler<Void>)state[1];
            var info = new java.net.http.HttpResponse.ResponseInfo() {
                public int statusCode() { return 200; }
                public java.net.http.HttpHeaders headers() { return java.net.http.HttpHeaders.of(Map.of(), (k, v) -> true); }
                public java.net.http.HttpClient.Version version() { return java.net.http.HttpClient.Version.HTTP_1_1; }
            };
            assertSame(error, assertThrows(IllegalArgumentException.class, () -> handler.apply(info)));
            assertEquals(StatusCode.OK, exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode());
            assertEquals(true, exporter.getFinishedSpanItems().get(0).getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
        } finally { JdkTransport.exit(state, null, error); }
    }
    @Test void jdkExactContentLengthAndEofAreCompleteHoweverClosed() {
        byte[] body = chunk("complete").getBytes(StandardCharsets.UTF_8);
        for (boolean eof : List.of(false, true)) {
            var capture = streamingCapture(eof ? Map.of("content-type", "text/event-stream") : Map.of("content-type", "text/event-stream", "content-length", String.valueOf(body.length)));
            capture.responseBytes(java.nio.ByteBuffer.wrap(body)); if (eof) capture.finish(null); capture.close();
            var span = exporter.getFinishedSpanItems().get(exporter.getFinishedSpanItems().size() - 1);
            assertEquals(StatusCode.OK, span.getStatus().getStatusCode()); assertNull(span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
        }
    }
    @Test void okhttpApplicationSourceExceptionIsConsumerAbort() throws Exception {
        var source = new TestSource(); source.applicationFailure = new IllegalStateException("app source failure");
        try (var response = new OkHttpTransport().intercept(new TestChain(source))) {
            var received = new Buffer(); assertTrue(response.body().source().read(received, 8192) > 0);
            assertSame(source.applicationFailure, assertThrows(IllegalStateException.class, () -> response.body().source().read(received, 8192)));
            var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
            assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
        }
    }
    @Test void okhttpApplicationSourceIOExceptionIsConsumerAbortAndOriginalSurvives() throws Exception {
        var source = new TestSource(); source.applicationFailure = new IOException("application transform deliberately aborted");
        try (var response = new OkHttpTransport().intercept(new TestChain(source))) {
            var received = new Buffer(); assertTrue(response.body().source().read(received, 8192) > 0);
            assertSame(source.applicationFailure, assertThrows(IOException.class, () -> response.body().source().read(received, 8192)));
            var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
            assertEquals(true, span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete"))); assertNull(span.getAttributes().get(AttributeKey.stringKey("error.type")));
        }
    }
    @Test void okhttpReadExceptionAfterExactLengthIsComplete() {
        var body = chunk("complete").getBytes(StandardCharsets.UTF_8);
        var capture = streamingCapture(Map.of("content-type", "text/event-stream", "content-length", String.valueOf(body.length)));
        capture.responseBytes(java.nio.ByteBuffer.wrap(body)); capture.readFailure(new IOException("app read after complete"));
        var span = exporter.getFinishedSpanItems().get(0); assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
        assertNull(span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
    }
    @Test void okhttpExactLengthIsCompleteEvenWhenCloseThrowsAndUnknownLengthEofIsComplete() throws Exception {
        for (boolean eof : List.of(false, true)) {
            var source = new TestSource(); source.failClose = true;
            var chain = new TestChain(source); if (!eof) chain.length = source.bytes.size();
            var response = new OkHttpTransport().intercept(chain); var received = new Buffer();
            assertTrue(response.body().source().read(received, 8192) > 0);
            if (eof) assertEquals(-1, response.body().source().read(received, 8192));
            assertThrows(IOException.class, () -> response.body().source().close());
            var span = exporter.getFinishedSpanItems().get(exporter.getFinishedSpanItems().size() - 1);
            assertEquals(StatusCode.OK, span.getStatus().getStatusCode()); assertNull(span.getAttributes().get(AttributeKey.booleanKey("traceai.wire.incomplete")));
        }
    }
    @Test void concurrentEqualJdkRequestsRegisterRetrySecretsProcessWide() throws Exception {
        var pool = Executors.newFixedThreadPool(2); var ready = new CountDownLatch(2); var release = new CountDownLatch(1);
        try {
            List<Future<?>> jobs = new ArrayList<>();
            for (int i = 0; i < 2; i++) {
                final int index = i;
                jobs.add(pool.submit(() -> {
                    var capture = streamingCapture(Map.of("content-type", "application/json"));
                    var source = java.net.http.HttpRequest.newBuilder(java.net.URI.create(capture.request.url)).POST(java.net.http.HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
                    var internal = java.net.http.HttpRequest.newBuilder(source, (k, v) -> true).build();
                    JdkTransport.header(internal, "Authorization", "Token RETRY_SECRET_" + index); ready.countDown();
                    try { assertTrue(release.await(3, TimeUnit.SECONDS)); } catch (InterruptedException error) { throw new AssertionError(error); }
                    var privacy = ExportPrivacy.privacy(TraceAIAuto.config());
                    assertEquals("<redacted>", privacy.text("RETRY_SECRET_" + index));
                    assertEquals("<redacted>", privacy.text("RETRY_SECRET_" + (1 - index)));
                    capture.close();
                }));
            }
            assertTrue(ready.await(3, TimeUnit.SECONDS)); release.countDown(); for (var job : jobs) job.get(3, TimeUnit.SECONDS);
        } finally { release.countDown(); pool.shutdownNow(); }
    }
    @Test void inFlightCaptureKeepsItsSingleRuntime() {
        WireCapture capture = TraceAIAuto.begin("POST", "https://api.openai.com/v1/chat/completions", Map.of("content-type", "application/json")); assertNotNull(capture);
        capture.requestBytes(java.nio.ByteBuffer.wrap(PlainApp.REQUEST.getBytes(StandardCharsets.UTF_8))); capture.requestComplete();
        assertSame(runtime.get(), capture.runtime);
        capture.responseHeaders(200, Map.of("content-type", "text/event-stream")); capture.responseBytes(java.nio.ByteBuffer.wrap(chunk("kept").getBytes(StandardCharsets.UTF_8))); capture.finish(null);
        assertEquals(1, exporter.getFinishedSpanItems().size()); assertEquals("kept", exporter.getFinishedSpanItems().get(0).getAttributes().get(AttributeKey.stringKey("output.value")));
    }
    @Test void shutdownDrainsAdmittedCompletionsWhileHonoringItsCallerDeadline() throws Exception {
        var capture = streamingCapture(Map.of("content-type", "text/event-stream"));
        capture.responseBytes(java.nio.ByteBuffer.wrap(chunk("kept").getBytes(StandardCharsets.UTF_8)));
        var state = runtime.get(); assertTrue(state.startFinalizing());
        try {
            long start = System.nanoTime(); TraceAIAuto.shutdown(java.time.Duration.ofMillis(40));
            assertTrue(System.nanoTime() - start < TimeUnit.SECONDS.toNanos(1));
            assertFalse(state.termination.isDone(), "shutdown must not close the exporter under admitted completion work");
            capture.finish(null);
            assertEquals(1, exporter.getFinishedSpanItems().size());
            assertEquals("kept", exporter.getFinishedSpanItems().get(0).getAttributes().get(AttributeKey.stringKey("output.value")));
        } finally { state.endFinalizing(); }
        state.termination.join(2, TimeUnit.SECONDS); assertTrue(state.termination.isSuccess());
    }
    @Test void shutdownCleanupIsNotLostBehindBlockedLifecycleCallback() throws Exception {
        var entered = new CountDownLatch(1); var release = new CountDownLatch(1); var state = runtime.get();
        Lifecycle.run(() -> { entered.countDown(); try { release.await(3, TimeUnit.SECONDS); } catch (InterruptedException error) { Thread.currentThread().interrupt(); } return io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess(); }, java.time.Duration.ofSeconds(3), false);
        assertTrue(entered.await(1, TimeUnit.SECONDS)); long start = System.nanoTime();
        try { TraceAIAuto.shutdown(java.time.Duration.ofMillis(70)); assertTrue(System.nanoTime() - start < TimeUnit.SECONDS.toNanos(1)); assertSame(state, runtime.get()); assertNull(TraceAIAuto.currentRuntime()); }
        finally { release.countDown(); }
        state.termination.join(2, TimeUnit.SECONDS); assertTrue(state.termination.isSuccess(), "cleanup must eventually run even after caller deadline");
    }
    @Test void queuedCleanupStillRunsAfterItsOwnDeadline() throws Exception {
        var entered = new CountDownLatch(1); var release = new CountDownLatch(1); var cleaned = new CountDownLatch(1);
        Lifecycle.cleanup(() -> { entered.countDown(); try { release.await(3, TimeUnit.SECONDS); } catch (InterruptedException error) { Thread.currentThread().interrupt(); } return io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess(); }, java.time.Duration.ofMillis(20), false);
        assertTrue(entered.await(1, TimeUnit.SECONDS));
        try { Lifecycle.cleanup(() -> { cleaned.countDown(); return io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess(); }, java.time.Duration.ofMillis(20), true); assertEquals(1, cleaned.getCount()); }
        finally { release.countDown(); }
        assertTrue(cleaned.await(2, TimeUnit.SECONDS));
    }
    @Test void unicodeProjectNamesUseHeaderSafeUtf8AndPreserveResourceName() throws Exception {
        var project = new AtomicReference<String>(); var payload = new AtomicReference<String>();
        var server = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> { project.set(exchange.getRequestHeaders().getFirst("fi-project-name")); payload.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)); exchange.sendResponseHeaders(200, -1); exchange.close(); }); server.start();
        try {
            AutoConfig config = AutoConfig.resolve(Map.of("api_key", "code-api", "secret_key", "code-secret", "project_name", "项目", "base_url", "http://127.0.0.1:" + server.getAddress().getPort()), Map.of());
            var memory = InMemorySpanExporter.create(); var unicode = SdkTracerProvider.builder().setResource(TraceAIAuto.resource(config)).addSpanProcessor(SimpleSpanProcessor.create(memory)).build();
            try {
                unicode.get("test").spanBuilder("test").startSpan().end(); assertTrue(new OtlpJsonExporter(config).export(memory.getFinishedSpanItems()).isSuccess());
                assertEquals("项目", java.net.URLDecoder.decode(project.get(), StandardCharsets.UTF_8)); assertTrue(payload.get().contains("项目"));
            } finally { unicode.shutdown(); }
        } finally { server.stop(0); }
    }
    @Test void exporterAuthenticationUsesResolvedArgumentsNotEnvironment() throws Exception {
        AtomicReference<String> api = new AtomicReference<>(), secret = new AtomicReference<>();
        var server = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> { api.set(exchange.getRequestHeaders().getFirst("X-Api-Key")); secret.set(exchange.getRequestHeaders().getFirst("X-Secret-Key")); exchange.getRequestBody().readAllBytes(); exchange.sendResponseHeaders(200, -1); exchange.close(); }); server.start();
        try {
            AutoConfig config = AutoConfig.resolve(Map.of("api_key", "code-api", "secret_key", "code-secret", "base_url", "http://127.0.0.1:" + server.getAddress().getPort()), Map.of("FI_API_KEY", "env-api", "FI_SECRET_KEY", "env-secret", "FI_BASE_URL", "not-a-url"));
            provider.get("test").spanBuilder("test").startSpan().end();
            assertTrue(new OtlpJsonExporter(config).export(exporter.getFinishedSpanItems()).isSuccess()); assertEquals("code-api", api.get()); assertEquals("code-secret", secret.get());
        } finally { server.stop(0); }
    }
}
