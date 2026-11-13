package ai.traceai.auto;

import org.junit.jupiter.api.Test;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class AdversarialTest {
    static WireCore.Request request(String url, Object json) {
        WireCore.Request request = new WireCore.Request("POST", url);
        request.headers.put("content-type", "application/json");
        if (json != null) request.body = WireFixturesTest.bytes(json);
        return request;
    }
    static WireCore.Response response(String type, String text) {
        WireCore.Response response = new WireCore.Response(); response.status = 200;
        response.headers.put("content-type", type); response.body = text.getBytes(StandardCharsets.UTF_8); return response;
    }
    static WireCore.Result build(WireCore.Request request, WireCore.Response response) {
        return WireCore.build(Objects.requireNonNull(WireCore.match(request.method, request.url)), request, response);
    }
    static WireCore.Request chat(String host) { return request("https://" + host + "/v1/chat/completions", Map.of("model", "m", "messages", List.of())); }
    @Test void responsesAcceptsInputWithoutModel() {
        assertNotNull(build(request("https://api.openai.com/v1/responses", Map.of("input", "hi")), response("application/json", "{}")));
    }
    @Test void unavailableAndTruncatedAreRestrictedToKnownProviders() {
        WireCore.Request known = chat("api.openai.com"); known.body = null;
        assertNotNull(build(known, response("application/json", "{}")));
        WireCore.Request proxy = chat("proxy.example"); proxy.body = null; proxy.truncated = true;
        assertNull(build(proxy, response("application/json", "{}")));
    }
    @Test void connectionFailureRetainsKnownProviderWithoutRequestBytes() {
        WireCore.Request request = chat("api.openai.com"); request.body = null;
        WireCore.Response response = new WireCore.Response(); response.error = new java.net.ConnectException("refused");
        WireCore.Result result = build(request, response); assertNotNull(result); assertFalse(result.ok());
        assertEquals("ConnectException", result.attributes().get("error.type"));
    }
    @Test void multipartValidationCannotBeBypassedByTruncation() {
        WireCore.Request request = request("https://api.openai.com/v1/audio/transcriptions", null);
        request.fields = Map.of("unrelated", "form"); request.truncated = true;
        assertNull(build(request, response("application/json", "{}")));
        request.url = "https://app.example/v1/chat/completions"; request.fields = Map.of("model", "m"); request.truncated = false;
        assertNull(build(request, response("application/json", "{}")));
    }
    @Test void multipartTextLimitIsPerFieldAndIndependentOfBodyLimit() {
        BodyCapture capture = new BodyCapture("multipart/form-data; boundary=b", 10, false);
        String body = "--b\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n" + "p".repeat(70000) + "\r\n--b\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--b--\r\n";
        capture.add(ByteBuffer.wrap(body.getBytes(StandardCharsets.UTF_8)));
        assertEquals("whisper-1", capture.multipart.fields.get("model"));
        assertEquals(65536, capture.multipart.fields.get("prompt").length()); assertTrue(capture.truncated);
    }
    @Test void multipartMetadataHasAnAggregateBound() {
        BodyCapture capture = new BodyCapture("multipart/form-data; boundary=b", 4194304, false);
        for (int i = 0; i < 1000; i++) capture.add(ByteBuffer.wrap(((i == 0 ? "--b" : "") + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" + i + "\"\r\n\r\nx\r\n--b").getBytes(StandardCharsets.UTF_8)));
        assertTrue(capture.multipart.files.size() <= 128); assertTrue(capture.truncated); assertEquals(1000, capture.multipart.fileBytes);
    }
    @Test void privacyCoversEveryMediaKeyCredentialsAndSignedUrls() {
        String media = "ABcd".repeat(100);
        Map<String, Object> content = new LinkedHashMap<>(); content.put("data", media); content.put("bytes", media); content.put("file_data", media); content.put("images", List.of(media));
        content.put("text", "look data:image/png;charset=utf-8;base64," + media); content.put("private-key", "private-value"); content.put("password", "password-value");
        content.put("url", "https://blob.example/a?X-Amz-Signature=signed-secret&key=query-secret&ok=1");
        WireCore.Request request = chat("api.openai.com"); request.body = WireFixturesTest.bytes(Map.of("messages", List.of(Map.of("role", "user", "content", List.of(Map.of("type", "text", "text", WireCore.dump(content))))), "extras", content));
        WireCore.Result result = build(request, response("application/json", "{}")); assertNotNull(result);
        String recorded = result.attributes().toString();
        for (String secret : List.of(media, "private-value", "password-value", "signed-secret", "query-secret")) assertFalse(recorded.contains(secret), secret);
        assertTrue(recorded.contains("<omitted: 400 base64 chars>"));
    }
    @Test void headerSecretsAreRemovedFromErrorsAndAllAttributes() {
        for (String header : List.of("Authorization", "xi-api-key", "api-key", "x-goog-api-key", "Ocp-Apim-Subscription-Key")) {
            WireCore.Request request = chat("api.openai.com"); request.headers.put(header, header.equals("Authorization") ? "Token SECRET_123456" : "SECRET_123456");
            WireCore.Response response = response("application/json", "{\"error\":{\"message\":\"echo SECRET_123456\"}}"); response.status = 401;
            assertEquals("echo <redacted>", build(request, response).description());
        }
    }
    @Test void requestParametersExcludeEveryContentKey() {
        WireCore.Request request = chat("api.openai.com"); Map<String, Object> json = new LinkedHashMap<>(WireCore.map(WireCore.parse(new String(request.body, StandardCharsets.UTF_8))));
        for (String key : List.of("audio_url", "url", "file_url", "image_url", "instructions")) json.put(key, "private-content");
        request.body = WireFixturesTest.bytes(json);
        assertFalse(String.valueOf(build(request, response("application/json", "{}")).attributes().get("gen_ai.request.parameters")).contains("private-content"));
    }
    @Test void partialStreamOutputIsPreservedOnTransportError() {
        WireCore.Response response = response("text/event-stream", "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"}}]}\n\n");
        response.error = new java.net.SocketTimeoutException("timeout");
        WireCore.Result result = build(chat("api.openai.com"), response); assertFalse(result.ok()); assertEquals("partial", result.attributes().get("output.value"));
    }
    @Test void hideFlagsCoverTranscriptsMessagesToolsAndParameters() {
        WireCore.Match tts = WireCore.match("POST", "https://api.openai.com/v1/audio/speech");
        WireCore.Match stt = WireCore.match("POST", "https://api.openai.com/v1/audio/transcriptions");
        for (String flag : List.of("hide_inputs", "hide_input_text")) {
            AutoConfig config = AutoConfig.resolve(Map.of(flag, true), Map.of());
            for (String key : List.of("gen_ai.audio.transcript", "input.value", "gen_ai.request.parameters", "gen_ai.tool.definitions.0.tool.json_schema", "gen_ai.input.messages.0.message.tool_calls.0.tool_call.function.arguments")) assertTrue(TraceAIAuto.hidden(key, tts, config), flag + " " + key);
            assertFalse(TraceAIAuto.hidden("gen_ai.audio.transcript", stt, config));
        }
        for (String flag : List.of("hide_outputs", "hide_output_text")) {
            AutoConfig config = AutoConfig.resolve(Map.of(flag, true), Map.of());
            assertTrue(TraceAIAuto.hidden("gen_ai.audio.transcript", stt, config)); assertFalse(TraceAIAuto.hidden("gen_ai.audio.transcript", tts, config));
            assertTrue(TraceAIAuto.hidden("gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments", tts, config));
        }
    }
    @Test void contextArraysAndConfiguredKeysAreScrubbedRecursively() {
        AutoConfig config = AutoConfig.resolve(Map.of("api_key", "FI_SECRET_123", "secret_key", "FI_OTHER_123"), Map.of());
        WirePrivacy privacy = new WirePrivacy(Map.of("Authorization", "Token REQUEST_SECRET"), config);
        Object clean = privacy.clean(List.of("echo REQUEST_SECRET", List.of("FI_SECRET_123"), Map.of("private_key", "private-value"), "FI_OTHER_123"));
        String recorded = WireCore.dump(clean);
        for (String secret : List.of("REQUEST_SECRET", "FI_SECRET_123", "FI_OTHER_123", "private-value")) assertFalse(recorded.contains(secret));
    }
    @Test void explicitValidValuesBeatInvalidEnvironmentAndResourceDefaultsMatchRegister() {
        AutoConfig config = AutoConfig.resolve(Map.of("sample_rate", .5, "wire_max_body_bytes", 100, "api_key", "code-key", "secret_key", "code-secret", "project_type", "experiment"), Map.of("FI_SAMPLE_RATE", "broken", "FI_WIRE_MAX_BODY_BYTES", "broken", "FI_API_KEY", "environment", "FI_PROJECT_TYPE", "bad"));
        assertEquals(.5, config.sampleRate); assertEquals(100, config.maxBodyBytes); assertEquals("code-key", config.get("FI_API_KEY")); assertEquals("experiment", config.get("FI_PROJECT_TYPE"));
        var attributes = TraceAIAuto.resource(config).getAttributes();
        for (String key : List.of("project_name", "project_type", "project_version_name", "project_version_id", "eval_tags", "metadata", "semantic_convention")) assertNotNull(attributes.get(io.opentelemetry.api.common.AttributeKey.stringKey(key)), key);
        assertEquals("DEFAULT_PROJECT_VERSION_NAME", attributes.get(io.opentelemetry.api.common.AttributeKey.stringKey("project_version_name")));
        assertEquals("[]", attributes.get(io.opentelemetry.api.common.AttributeKey.stringKey("eval_tags"))); assertEquals("{}", attributes.get(io.opentelemetry.api.common.AttributeKey.stringKey("metadata")));
        assertEquals(java.nio.file.Path.of(System.getProperty("user.dir")).getFileName().toString(), config.get("FI_PROJECT_NAME"));
        assertEquals("service", AutoConfig.resolve(Map.of(), Map.of("OTEL_SERVICE_NAME", "service")).get("FI_PROJECT_NAME"));
    }
    @Test void requestPublisherIsNeverSubscribedBeforeTheRealTransport() {
        java.util.concurrent.atomic.AtomicInteger subscriptions = new java.util.concurrent.atomic.AtomicInteger();
        var publisher = new java.net.http.HttpRequest.BodyPublisher() {
            public long contentLength() { return -1; }
            public void subscribe(java.util.concurrent.Flow.Subscriber<? super ByteBuffer> subscriber) { subscriptions.incrementAndGet(); }
        };
        var request = java.net.http.HttpRequest.newBuilder(java.net.URI.create("https://api.openai.com/v1/chat/completions")).POST(publisher).build();
        WireCapture capture = new WireCapture(chat("api.openai.com"), Map.of(), WireCore.match("POST", request.uri().toString()), OtelBridge.snapshot(), 100);
        var wrapped = new JdkTransport.CapturedRequest(request, capture); wrapped.bodyPublisher().orElseThrow().contentLength();
        assertEquals(0, subscriptions.get()); assertEquals(request.timeout(), wrapped.timeout());
    }
    @Test void emptyChunksDoNotFinalizeAndConsumerCancellationMarksIncompleteOnce() throws Exception {
        WireCapture capture = new WireCapture(chat("api.openai.com"), Map.of("content-type", "application/json"), WireCore.match("POST", "https://api.openai.com/v1/chat/completions"), OtelBridge.snapshot(), 1024);
        capture.requestComplete(); capture.responseHeaders(200, Map.of("content-type", "text/event-stream"));
        var subscriber = JdkTransport.subscriber(java.net.http.HttpResponse.BodySubscribers.ofInputStream(), capture);
        subscriber.onSubscribe(new java.util.concurrent.Flow.Subscription() { public void request(long n) {} public void cancel() {} });
        subscriber.onNext(List.of(ByteBuffer.allocate(0))); assertFalse(capture.response.incomplete);
        subscriber.getBody().toCompletableFuture().join().close(); assertTrue(capture.response.incomplete);
        capture.finish(new java.net.SocketTimeoutException("late")); assertNull(capture.response.error);
    }
    @Test void lifecycleDeadlineBoundsBlockingAndReentrantCallbacks() throws Exception {
        var entered = new java.util.concurrent.CountDownLatch(1); var release = new java.util.concurrent.CountDownLatch(1);
        long start = System.nanoTime();
        try {
            Lifecycle.run(() -> { entered.countDown(); try { release.await(2, java.util.concurrent.TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } return io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess(); }, java.time.Duration.ofMillis(50), true);
            assertTrue(entered.await(1, java.util.concurrent.TimeUnit.SECONDS)); assertTrue(System.nanoTime() - start < 500_000_000L);
        } finally { release.countDown(); }
        var called = new java.util.concurrent.atomic.AtomicBoolean();
        Lifecycle.run(() -> { Lifecycle.run(() -> { fail("reentrant lifecycle callback"); return null; }, java.time.Duration.ofSeconds(1), true); called.set(true); return io.opentelemetry.sdk.common.CompletableResultCode.ofSuccess(); }, java.time.Duration.ofSeconds(1), true);
        assertTrue(called.get());
    }
    @Test void decompressionBombIsBounded() throws Exception {
        WireCore.Response response = response("application/json", " ".repeat(100000)); response.limit = 1024;
        var out = new java.io.ByteArrayOutputStream(); try (var gzip = new java.util.zip.GZIPOutputStream(out)) { gzip.write(response.body); }
        response.body = out.toByteArray(); response.headers.put("content-encoding", "gzip");
        assertEquals(true, build(chat("api.openai.com"), response).attributes().get("traceai.wire.truncated"));
    }
    @Test void deflateResponsesAreDecodedAndUnknownEncodingIsMarked() throws Exception {
        WireCore.Response response = response("application/json", "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"decoded\"}}]}");
        var out = new java.io.ByteArrayOutputStream(); try (var zip = new java.util.zip.DeflaterOutputStream(out)) { zip.write(response.body); }
        response.body = out.toByteArray(); response.headers.put("content-encoding", "deflate");
        assertEquals("decoded", build(chat("api.openai.com"), response).attributes().get("output.value"));
        response.headers.put("content-encoding", "unknown");
        assertEquals("unknown", build(chat("api.openai.com"), response).attributes().get("traceai.wire.undecoded_encoding"));
    }
}
