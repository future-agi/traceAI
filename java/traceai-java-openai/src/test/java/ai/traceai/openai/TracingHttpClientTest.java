package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.core.RequestOptions;
import com.openai.core.http.*;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import java.io.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import static org.assertj.core.api.Assertions.*;

/** The same transport contracts execute with the floor and current SDK jars. No vendor access. */
class TracingHttpClientTest {
    final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    final SdkTracerProvider provider = SdkTracerProvider.builder()
        .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    final FITracer tracer = new FITracer(provider.get("test"), TraceConfig.builder().build());
    @AfterEach void closeProvider() { provider.close(); }

    @Test void wrappingDoesNotReadAndCloseIsOwnedByCaller() throws Exception {
        var raw = new FakeResponse("application/json", "{\"id\":\"r1\",\"model\":\"m1\"}");
        var transport = new FakeTransport(raw);
        var response = TracingHttpClient.wrap(transport, tracer).execute(request("responses", "{\"model\":\"m\"}"));
        assertThat(raw.reads.get()).isZero();
        assertThat(raw.closes.get()).isZero();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        assertThat(response.headers()).isSameAs(raw.headers());
        assertThat(response.body().read()).isEqualTo('{');
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        response.close();
        response.close();
        assertThat(raw.closes.get()).isEqualTo(1);
        assertThat(spans()).hasSize(1);
        assertThat(spans().get(0).getAttributes().get(AttributeKey.booleanKey("traceai.response.closed_early"))).isTrue();
    }

    @Test void eofRecordsModelsUsageRolesAndTheOriginalParent() throws Exception {
        String json = "{\"id\":\"r1\",\"model\":\"served\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3,\"total_tokens\":5}}";
        var raw = new FakeResponse("application/json", json);
        var parent = provider.get("test").spanBuilder("parent").startSpan();
        HttpResponse response;
        var fake = new FakeTransport(raw);
        try (var scope = parent.makeCurrent()) {
            response = TracingHttpClient.wrap(fake, tracer).execute(request("chat/completions",
                "{\"model\":\"requested\",\"messages\":[{\"role\":\"system\",\"content\":\"rules\"},{\"role\":\"user\",\"content\":\"question\"}]}"));
        }
        assertThat(new String(response.body().readAllBytes(), StandardCharsets.UTF_8)).isEqualTo(json);
        response.close();
        assertThat(spans()).hasSize(1);
        SpanData span = spans().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(fake.spanDuringExecute).isEqualTo(span.getSpanId());
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(attr(span, "gen_ai.input.messages")).contains("system", "rules", "user", "question");
        assertThat(attr(span, "gen_ai.request.model")).isEqualTo("requested");
        assertThat(attr(span, "gen_ai.response.model")).isEqualTo("served");
        assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(5);
        parent.end();
    }

    @Test void sseIsFragmentSafeAndIncludesFinalUsage() throws Exception {
        String sse = "data: {\"id\":\"c1\",\"model\":\"m\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hëllo\"}}]}\r\n\r\n"
            + "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":2,\"total_tokens\":9}}\n\ndata: [DONE]\n\n";
        var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("text/event-stream", sse)), tracer)
            .execute(request("chat/completions", "{}"));
        var bytes = new ByteArrayOutputStream();
        int b;
        while ((b = response.body().read()) != -1) bytes.write(b);
        response.close();
        assertThat(bytes.toString(StandardCharsets.UTF_8)).isEqualTo(sse);
        assertThat(spans()).hasSize(1);
        assertThat(attr(spans().get(0), "output.value")).isEqualTo("hëllo");
        assertThat(spans().get(0).getAttributes().get(AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(9);
    }

    @Test void responsesSseCompletionAndFailureAreObserved() throws Exception {
        String sse = "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\"}\n\n"
            + "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"model\":\"m\",\"usage\":{\"input_tokens\":2,\"output_tokens\":3,\"total_tokens\":5}}}\n\n";
        try (var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("text/event-stream", sse)), tracer)
                .execute(request("responses", "{}"))) {
            assertThat(response.body().readAllBytes()).isEqualTo(sse.getBytes(StandardCharsets.UTF_8));
        }
        assertThat(attr(spans().get(0), "output.value")).isEqualTo("hello");
        assertThat(attr(spans().get(0), "gen_ai.response.id")).isEqualTo("r1");
        assertThat(spans().get(0).getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens"))).isEqualTo(2);
        try (var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("text/event-stream",
                "data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"message\":\"private\"}}}\n\n")), tracer)
                .execute(request("responses", "{}"))) { response.body().readAllBytes(); }
        assertThat(spans().get(1).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test void captureHasABoundedBudgetAndDoesNotTruncateCallerData() throws Exception {
        String delta = "data: {\"type\":\"response.output_text.delta\",\"delta\":\"" + "x".repeat(1000) + "\"}\n\n";
        String body = delta.repeat(120) + "data: [DONE]\n\n";
        try (var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("text/event-stream", body)), tracer)
                .execute(request("responses", "{}"))) {
            assertThat(response.body().readAllBytes()).hasSize(body.length());
        }
        assertThat(attr(spans().get(0), "output.value").length()).isLessThanOrEqualTo(32000);
        assertThat(spans().get(0).getAttributes().get(AttributeKey.booleanKey("traceai.capture.truncated"))).isTrue();
    }

    @Test void oversizedOrMalformedPayloadDoesNotChangeTheResponse() throws Exception {
        String body = "{\"output\":\"" + "x".repeat(100000) + "\"}";
        for (String payload : List.of(body, "not-json", "[1,2]")) {
            try (var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("application/json", payload)), tracer)
                    .execute(request("responses", "{}"))) {
                assertThat(new String(response.body().readAllBytes(), StandardCharsets.UTF_8)).isEqualTo(payload);
            }
        }
        assertThat(spans()).hasSize(3);
    }

    @Test void requestIsWrittenOnceWithHeadersAndOptionsUnchanged() throws Exception {
        var fake = new FakeTransport(new FakeResponse("application/json", "{}"));
        var params = request("embeddings", "{\"input\":\"text\",\"model\":\"embed\"}");
        RequestOptions options = RequestOptions.none();
        try (var response = TracingHttpClient.wrap(fake, tracer).execute(params, options)) {
            response.body().readAllBytes();
        }
        assertThat(fake.requestBytes).isEqualTo("{\"input\":\"text\",\"model\":\"embed\"}");
        assertThat(fake.options).isSameAs(options);
        assertThat(fake.headers).isEqualTo(params.headers());
        assertThat(fake.writes).isEqualTo(1);
    }

    @Test void unsupportedOperationsPassThroughWithoutASpan() throws Exception {
        var raw = new FakeResponse("application/json", "{}");
        var transport = new FakeTransport(raw);
        var client = TracingHttpClient.wrap(transport, tracer);
        assertThat(client.execute(request("files", "{}"))).isSameAs(raw);
        assertThat(spans()).isEmpty();
        assertThat(TracingHttpClient.wrap(client, tracer)).isSameAs(client);
        client.close();
        assertThat(transport.closed).isTrue();
    }

    @Test void synchronousFailurePreservesItsIdentityAndErrorPrivacy() throws Exception {
        var failure = new IllegalStateException("private-detail");
        var transport = new FakeTransport(null) {
            @Override public HttpResponse execute(HttpRequest r, RequestOptions o) { throw failure; }
        };
        var privateTracer = new FITracer(provider.get("test"), TraceConfig.builder().hideErrorDetails(true).build());
        assertThatThrownBy(() -> TracingHttpClient.wrap(transport, privateTracer).execute(request("responses", "{}"))).isSameAs(failure);
        assertThat(spans()).hasSize(1);
        assertThat(spans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(spans().get(0).toString()).doesNotContain("private-detail");
    }

    @Test void readAndCloseFailuresAreNotSwallowed() throws Exception {
        var io = new IOException("read-failure");
        var raw = new FakeResponse("application/json", "{}");
        raw.body = new InputStream() { @Override public int read() throws IOException { throw io; } };
        var response = TracingHttpClient.wrap(new FakeTransport(raw), tracer).execute(request("responses", "{}"));
        assertThatThrownBy(() -> response.body().read()).isSameAs(io);
        response.close();
        assertThat(spans()).hasSize(1);
        assertThat(spans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        var closing = new IllegalStateException("close-failure");
        var badClose = new FakeResponse("application/json", "{}") {
            @Override public void close() { throw closing; }
        };
        var response2 = TracingHttpClient.wrap(new FakeTransport(badClose), tracer).execute(request("responses", "{}"));
        assertThatThrownBy(response2::close).isSameAs(closing);
        assertThat(spans().get(1).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test void httpErrorsRetainTheirStatus() throws Exception {
        var raw = new FakeResponse("application/json", "{\"error\":{\"message\":\"bad\"}}");
        raw.status = 429;
        try (var response = TracingHttpClient.wrap(new FakeTransport(raw), tracer).execute(request("responses", "{}"))) {
            assertThat(response.statusCode()).isEqualTo(429);
            response.body().readAllBytes();
        }
        assertThat(spans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test void asyncCallbacksAndBodyReadsRunInTheRequestContext() throws Exception {
        var pending = new CompletableFuture<HttpResponse>();
        var transport = new FakeTransport(null) {
            @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) { return pending; }
        };
        var parent = provider.get("test").spanBuilder("parent").startSpan();
        CompletableFuture<HttpResponse> result;
        try (var scope = parent.makeCurrent()) {
            result = TracingHttpClient.wrap(transport, tracer).executeAsync(request("responses", "{}"));
        }
        var callbackSpan = result.thenApply(r -> Span.current().getSpanContext().getSpanId());
        var raw = new FakeResponse("application/json", "{}");
        var executor = Executors.newSingleThreadExecutor();
        try { executor.submit(() -> pending.complete(raw)).get(5, TimeUnit.SECONDS); }
        finally { executor.shutdownNow(); }
        assertThat(spans()).isEmpty();
        try (var response = result.get(5, TimeUnit.SECONDS)) { response.body().readAllBytes(); }
        assertThat(spans().get(0).getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(callbackSpan.get()).isEqualTo(spans().get(0).getSpanId());
        assertThat(raw.spanDuringRead).isEqualTo(spans().get(0).getSpanId());
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
        parent.end();
    }

    @Test void asyncCancellationPropagatesAndClosesALateResponse() throws Exception {
        AtomicInteger cancelled = new AtomicInteger();
        var pending = new CompletableFuture<HttpResponse>() {
            @Override public boolean cancel(boolean interrupt) { cancelled.incrementAndGet(); return false; }
        };
        var transport = new FakeTransport(null) {
            @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) { return pending; }
        };
        var result = TracingHttpClient.wrap(transport, tracer).executeAsync(request("responses", "{}"));
        assertThat(result.cancel(true)).isTrue();
        assertThat(cancelled.get()).isEqualTo(1);
        var raw = new FakeResponse("application/json", "{}");
        pending.complete(raw);
        assertThat(raw.closes.get()).isEqualTo(1);
        assertThat(raw.reads.get()).isZero();
        assertThat(spans()).hasSize(1);
    }

    @Test void asyncFailureKeepsTheOriginalCause() throws Exception {
        var error = new IllegalArgumentException("bad-request");
        var transport = new FakeTransport(null) {
            @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) {
                return CompletableFuture.failedFuture(error);
            }
        };
        var result = TracingHttpClient.wrap(transport, tracer).executeAsync(request("responses", "{}"));
        assertThatThrownBy(result::join).hasCause(error);
        assertThat(spans()).hasSize(1);
        assertThat(spans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test void privacyAndSpanShapeSurviveOtlpSerialization() throws Exception {
        var received = new LinkedBlockingQueue<byte[]>();
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            received.add(exchange.getRequestBody().readAllBytes());
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        var otlp = OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces").build();
        try (var sdk = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(otlp)).build()) {
            var privacy = TraceConfig.builder().hideInputs(true).hideOutputs(true).hideErrorDetails(true).build();
            var trace = new FITracer(sdk.get("wire"), privacy);
            var raw = new FakeResponse("application/json", "{\"id\":\"r1\",\"model\":\"model\",\"output\":\"output-secret\",\"usage\":{\"input_tokens\":2,\"output_tokens\":3,\"total_tokens\":5}}");
            try (var response = TracingHttpClient.wrap(new FakeTransport(raw), trace)
                    .execute(request("responses", "{\"model\":\"model\",\"input\":\"input-secret\"}"))) { response.body().readAllBytes(); }
            sdk.forceFlush().join(5, TimeUnit.SECONDS);
            byte[] wire = received.poll(5, TimeUnit.SECONDS);
            assertThat(wire).isNotNull();
            var message = ExportTraceServiceRequest.parseFrom(wire);
            assertThat(message.getResourceSpansCount()).isEqualTo(1);
            var spans = message.getResourceSpans(0).getScopeSpans(0).getSpansList();
            assertThat(spans).hasSize(1);
            String text = message.toString();
            assertThat(text).contains("openai", "responses", "gen_ai.usage.total_tokens", "int_value: 5")
                .doesNotContain("input-secret", "output-secret", "offline-not-a-key", "fi.raw_input", "fi.raw_output");
            assertThat(spans.get(0).getStatus().getCode().name()).isEqualTo("STATUS_CODE_OK");
        } finally { server.stop(0); }
    }

    @Test void messageHidingCannotBeBypassedThroughRawPayloads() throws Exception {
        var hidden = new FITracer(provider.get("test"), TraceConfig.builder().hideInputMessages(true).hideOutputMessages(true).build());
        try (var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("application/json",
                "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"output-secret\"}}]}")), hidden)
                .execute(request("chat/completions", "{\"messages\":[{\"role\":\"user\",\"content\":\"input-secret\"}]}"))) {
            response.body().readAllBytes();
        }
        assertThat(spans().get(0).toString()).doesNotContain("input-secret", "output-secret");
    }

    @Test void clientShutdownCancelsPendingAndClosesAbandonedResponses() throws Exception {
        var raw = new FakeResponse("application/json", "{}");
        var upstream = new CompletableFuture<HttpResponse>();
        var fake = new FakeTransport(raw) {
            @Override public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) { return upstream; }
        };
        var client = TracingHttpClient.wrap(fake, tracer);
        var response = client.execute(request("responses", "{}"));
        var future = client.executeAsync(request("responses", "{}"));
        client.close();
        client.close();
        response.close();
        assertThat(raw.closes.get()).isEqualTo(1);
        assertThat(upstream.isCancelled()).isTrue();
        assertThat(future.isCancelled()).isTrue();
        assertThat(fake.closed).isTrue();
        assertThat(spans()).hasSize(2);
    }

    @Test void errorPayloadsCannotBypassErrorDetailPrivacy() throws Exception {
        var privateTracer = new FITracer(provider.get("test"), TraceConfig.builder().hideErrorDetails(true).build());
        for (int status : new int[] {200, 429}) {
            var raw = new FakeResponse("application/json", "{\"error\":{\"message\":\"private-detail\"}}");
            raw.status = status;
            try (var response = TracingHttpClient.wrap(new FakeTransport(raw), privateTracer).execute(request("responses", "{}"))) {
                response.body().readAllBytes();
            }
        }
        assertThat(spans()).hasSize(2);
        for (var span : spans()) {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.toString()).doesNotContain("private-detail");
        }
    }

    @Test void prematureSseEofIsNotReportedAsSuccessfulGeneration() throws Exception {
        try (var response = TracingHttpClient.wrap(new FakeTransport(new FakeResponse("text/event-stream",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n")), tracer)
                .execute(request("responses", "{}"))) { response.body().readAllBytes(); }
        assertThat(spans()).hasSize(1);
        assertThat(spans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
        assertThat(spans().get(0).getAttributes().get(AttributeKey.booleanKey("traceai.response.closed_early"))).isTrue();
    }

    List<SpanData> spans() { return exporter.getFinishedSpanItems(); }
    static String attr(SpanData span, String key) { return span.getAttributes().get(AttributeKey.stringKey(key)); }

    static HttpRequest request(String path, String json) throws Exception {
        var builder = HttpRequest.builder().method(HttpMethod.POST).addPathSegments(path.split("/"));
        // Only a fixture bridge: production uses the SDK's request without inspecting client internals.
        try { builder.getClass().getMethod("baseUrl", String.class).invoke(builder, "http://127.0.0.1"); }
        catch (NoSuchMethodException oldSdk) { builder.getClass().getMethod("url", String.class).invoke(builder, "http://127.0.0.1"); }
        byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
        return builder.putHeader("Authorization", "Bearer offline-not-a-key").body(new HttpRequestBody() {
            public void writeTo(OutputStream out) { try { out.write(bytes); } catch (IOException e) { throw new UncheckedIOException(e); } }
            public String contentType() { return "application/json"; }
            public long contentLength() { return bytes.length; }
            public boolean repeatable() { return true; }
            public void close() {}
        }).build();
    }

    static class FakeTransport implements HttpClient {
        final HttpResponse response;
        String requestBytes;
        String spanDuringExecute;
        int writes;
        boolean closed;
        Headers headers;
        RequestOptions options;
        FakeTransport(HttpResponse response) { this.response = response; }
        public HttpResponse execute(HttpRequest request) { return execute(request, RequestOptions.none()); }
        public HttpResponse execute(HttpRequest request, RequestOptions options) {
            this.options = options;
            headers = request.headers();
            spanDuringExecute = Span.current().getSpanContext().getSpanId();
            if (request.body() != null) {
                var out = new ByteArrayOutputStream();
                try { request.body().writeTo(out); } catch (Exception e) { throw new IllegalStateException(e); }
                requestBytes = out.toString(StandardCharsets.UTF_8);
                writes++;
            }
            return response;
        }
        public CompletableFuture<HttpResponse> executeAsync(HttpRequest r) { return executeAsync(r, RequestOptions.none()); }
        public CompletableFuture<HttpResponse> executeAsync(HttpRequest r, RequestOptions o) { return CompletableFuture.completedFuture(execute(r, o)); }
        public void close() { closed = true; }
    }

    static class FakeResponse implements HttpResponse {
        final AtomicInteger reads = new AtomicInteger();
        final AtomicInteger closes = new AtomicInteger();
        final Headers headers;
        int status = 200;
        InputStream body;
        String spanDuringRead;
        FakeResponse(String contentType, String text) {
            headers = Headers.builder().put("content-type", contentType).put("x-request-id", "request-1").build();
            body = new ByteArrayInputStream(text.getBytes(StandardCharsets.UTF_8)) {
                @Override public synchronized int read(byte[] b, int off, int len) {
                    reads.incrementAndGet();
                    spanDuringRead = Span.current().getSpanContext().getSpanId();
                    return super.read(b, off, len);
                }
            };
        }
        public int statusCode() { return status; }
        public Headers headers() { return headers; }
        public InputStream body() { return body; }
        public void close() {
            closes.incrementAndGet();
            try { body.close(); } catch (IOException e) { throw new UncheckedIOException(e); }
        }
    }
}
