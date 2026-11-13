package ai.traceai.auto;

import java.net.URI;
import java.net.http.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.CompletableFuture;
import okhttp3.*;

/** The normal path contains no initialization, tracer calls, or agent API calls. */
public final class PlainApp {
    static final String REQUEST = "{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}";
    public static void main(String[] args) throws Exception {
        if (args[2].equals("resource_identity")) { ResourceApp.run(args); return; }
        if (args[2].equals("magic_media")) { MagicMediaApp.run(args); return; }
        if (args[2].startsWith("registry")) { RegistryApp.run(args); return; }
        if (args[2].startsWith("parity")) { ParityApp.run(args); return; }
        if (args[2].startsWith("reconfigure")) { ReconfigurationApp.run(args); return; }
        if (args[2].startsWith("coexist")) { CoexistApp.run(args); return; }
        if (args[2].startsWith("verify_headers")) { HeaderPrivacyApp.run(args); return; }
        if (args[2].equals("legacy_okhttp")) { legacyOkhttp(args); return; }
        if (args[2].equals("verify_wrapper") || args[2].equals("verify_parent") || args[2].equals("verify_cohere")) { VerifierApp.run(args); return; }
        if (args[2].startsWith("dedup") || args[2].startsWith("framework")) { ContextApp.run(args); return; }
        if (args[2].equals("suppressed")) {
            try (AutoCloseable scope = (AutoCloseable)Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("suppress").invoke(null)) { call(args); }
            return;
        }
        call(args);
    }
    static String request(String scenario) {
        if (scenario.equals("magic_media")) return MagicMediaApp.REQUEST;
        if (scenario.equals("registry_bodypasswd")) return REQUEST.substring(0, REQUEST.length() - 1) + ",\"passwd\":\"" + RegistryApp.BODY_SECRET + "\"}";
        if (scenario.equals("registry_bodyarray")) return REQUEST.substring(0, REQUEST.length() - 1) + ",\"token\":[\"" + RegistryApp.BODY_SECRET + "\"]}";
        if (scenario.startsWith("registry_body")) {
            String padding = scenario.equals("registry_body8200") ? "0,".repeat(8199) + "0" : "";
            return REQUEST.substring(0, REQUEST.length() - 1) + ",\"padding\":[" + padding + "],\"password\":\"" + RegistryApp.BODY_SECRET + "\"}";
        }
        if (scenario.equals("ollama_image")) return "{\"model\":\"test-model\",\"prompt\":\"Hello\",\"images\":[\"" + image() + "\"],\"options\":{\"temperature\":0.5}}";
        if (scenario.equals("data_url_async")) return "{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"" + "data:a".repeat(5461) + "\"}]}";
        if (scenario.equals("signed_query_async")) return "{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"" + "?".repeat(32768) + "\"}]}";
        if (scenario.equals("audio")) return "{\"model\":\"tts-1\",\"input\":\"Speak\",\"voice\":\"alloy\"}";
        if (scenario.equals("multipart_unlabelled")) return "--traceai-test\r\nContent-Disposition: form-data; name=model\r\n\r\nwhisper-1\r\n--traceai-test\r\nContent-Disposition: form-data; name=file\r\nContent-Type: text/plain\r\n\r\nPRIVATE_FILE_PAYLOAD\r\n--traceai-test--\r\n";
        if (scenario.equals("multipart")) return "--traceai-test\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--traceai-test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + "\0".repeat(102400) + "\r\n--traceai-test--\r\n";
        return REQUEST;
    }
    static String image() {
        try {
            var image = new java.awt.image.BufferedImage(1, 1, java.awt.image.BufferedImage.TYPE_INT_RGB);
            var bytes = new ByteArrayOutputStream(); javax.imageio.ImageIO.write(image, "png", bytes);
            return Base64.getEncoder().encodeToString(bytes.toByteArray());
        } catch (IOException error) { throw new java.io.UncheckedIOException(error); }
    }
    static void call(String[] args) throws Exception {
        byte[] received;
        String outgoing = request(args[2]), contentType = args[2].startsWith("multipart") ? "multipart/form-data; boundary=traceai-test" : "application/json";
        boolean early = args[2].equals("earlyreject");
        boolean transportFailure = args[2].startsWith("verify_transport");
        boolean corruptGzip = args[2].equals("verify_gzip") || args[2].equals("verify_gzip_abort");
        boolean image = args[2].equals("ollama_image");
        String endpoint = image ? "http://ollama.com/api/generate" : early || transportFailure ? "http://api.openai.com/v1/chat/completions" : args[1];
        java.net.InetSocketAddress proxy = early || transportFailure || image ? new java.net.InetSocketAddress("127.0.0.1", Integer.parseInt(System.getenv("TRACEAI_TEST_PROXY_PORT"))) : null;
        if (args[0].equals("jdk")) {
            var clientBuilder = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.ALWAYS); if (early || image) clientBuilder.proxy(java.net.ProxySelector.of(proxy));
            if (args[2].equals("authretry")) clientBuilder.authenticator(new java.net.Authenticator() {
                @Override protected java.net.PasswordAuthentication getPasswordAuthentication() { return new java.net.PasswordAuthentication("retry-user", "retry-password".toCharArray()); }
            });
            HttpClient client = clientBuilder.build();
            var requestBuilder = java.net.http.HttpRequest.newBuilder(URI.create(endpoint)).expectContinue(early).header("Content-Type", contentType).POST(java.net.http.HttpRequest.BodyPublishers.ofString(outgoing));
            if (args[2].contains("privacy")) requestBuilder.header("Authorization", "Bearer PROVIDER_CREDENTIAL_987654321");
            java.net.http.HttpRequest request = requestBuilder.build();
            if (args[2].equals("signed_query_async") || args[2].equals("data_url_async")) {
                long start = System.nanoTime(); boolean timedOut = false;
                var future = client.sendAsync(request, java.net.http.HttpResponse.BodyHandlers.ofString());
                java.net.http.HttpResponse<String> response;
                try { response = future.get(3, java.util.concurrent.TimeUnit.SECONDS); }
                catch (java.util.concurrent.TimeoutException expected) { timedOut = true; response = future.get(15, java.util.concurrent.TimeUnit.SECONDS); }
                System.out.println("APP_FUTURE_TIMEOUT=" + timedOut); System.out.println("SEND_MS=" + (System.nanoTime() - start) / 1_000_000);
                if (response.statusCode() != 200) throw new AssertionError("Unexpected HTTP status " + response.statusCode());
                received = response.body().getBytes(StandardCharsets.UTF_8);
            } else {
                java.net.http.HttpResponse<InputStream> response = args[2].equals("async3") ? client.sendAsync(request, java.net.http.HttpResponse.BodyHandlers.ofInputStream(), (initial, pushed, accept) -> {}).join() : args[2].endsWith("async") ? client.sendAsync(request, java.net.http.HttpResponse.BodyHandlers.ofInputStream()).join() : client.send(request, java.net.http.HttpResponse.BodyHandlers.ofInputStream());
                try (InputStream stream = response.body()) { received = copy(stream); }
            }
        } else {
            OkHttpClient.Builder builder = new OkHttpClient.Builder();
            if (early || transportFailure || image) builder.proxy(new java.net.Proxy(java.net.Proxy.Type.HTTP, proxy));
            IOException applicationFailure = new IOException("application transform deliberately aborted");
            if (args[2].equals("verify_abort") || args[2].equals("verify_gzip_abort")) builder.addNetworkInterceptor(chain -> {
                var response = chain.proceed(chain.request()); var original = response.body();
                return response.newBuilder().body(new ResponseBody() {
                    final okio.BufferedSource source = okio.Okio.buffer(new okio.ForwardingSource(original.source()) {
                        boolean read;
                        @Override public long read(okio.Buffer sink, long count) throws IOException {
                            if (args[2].equals("verify_gzip_abort")) {
                                long n = super.read(sink, count); if (n < 0) throw applicationFailure; return n;
                            }
                            if (read) throw applicationFailure;
                            long n = super.read(sink, Math.min(count, 1)); if (n > 0) read = true; return n;
                        }
                    });
                    public MediaType contentType() { return original.contentType(); }
                    public long contentLength() { return original.contentLength(); }
                    public okio.BufferedSource source() { return source; }
                }).build();
            });
            if (args[2].equals("verify_transport_wrapped") || args[2].equals("verify_transport_independent")) builder.addNetworkInterceptor(chain -> {
                var response = chain.proceed(chain.request()); var original = response.body();
                return response.newBuilder().body(new ResponseBody() {
                    final okio.BufferedSource source = okio.Okio.buffer(new okio.ForwardingSource(original.source()) {
                        @Override public long read(okio.Buffer sink, long count) throws IOException {
                            try { return super.read(sink, count); }
                            catch (IOException error) {
                                if (args[2].equals("verify_transport_wrapped")) throw new IOException("app adapter wrapping native failure", error);
                                throw applicationFailure;
                            }
                        }
                    });
                    public MediaType contentType() { return original.contentType(); }
                    public long contentLength() { return original.contentLength(); }
                    public okio.BufferedSource source() { return source; }
                }).build();
            });
            if (args[2].equals("verify_eager")) builder.addNetworkInterceptor(chain -> {
                var response = chain.proceed(chain.request()); response.body().source().read(new okio.Buffer(), 1); throw applicationFailure;
            });
            if (args[2].equals("authretry")) builder.authenticator((route, response) -> response.request().newBuilder().header("Authorization", "Token RETRY_SECRET_123").build());
            builder.build(); // reuse builder must not install a second interceptor
            OkHttpClient client = args[2].equals("defaultclient") ? new OkHttpClient() : builder.build();
            RequestBody outgoingBody = RequestBody.create(MediaType.get(contentType), outgoing);
            var getterCalls = new java.util.concurrent.atomic.AtomicInteger();
            if (args[2].startsWith("metadata")) outgoingBody = new RequestBody() {
                public MediaType contentType() {
                    if (getterCalls.incrementAndGet() > 1) throw new IllegalStateException("metadata already consumed");
                    return MediaType.get(contentType);
                }
                public void writeTo(okio.BufferedSink sink) throws IOException { sink.writeUtf8(outgoing); }
            };
            if (args[2].equals("verify_closing")) outgoingBody = new RequestBody() {
                public MediaType contentType() { return MediaType.get(contentType); }
                public long contentLength() { return outgoing.getBytes(StandardCharsets.UTF_8).length; }
                public void writeTo(okio.BufferedSink sink) throws IOException {
                    try (var stream = sink.outputStream()) { stream.write(outgoing.getBytes(StandardCharsets.UTF_8)); }
                }
            };
            okhttp3.Request.Builder requestBuilder = new okhttp3.Request.Builder().url(endpoint).post(outgoingBody);
            if (early) requestBuilder.header("Expect", "100-continue");
            if (args[2].contains("privacy")) requestBuilder.header("Authorization", "Bearer PROVIDER_CREDENTIAL_987654321");
            // Explicit negotiation leaves encoded bytes visible to the application and agent alike.
            if (args[2].equals("gzip")) requestBuilder.header("Accept-Encoding", "gzip");
            okhttp3.Request request = requestBuilder.build();
            if (args[2].endsWith("async")) {
                CompletableFuture<byte[]> body = new CompletableFuture<>();
                client.newCall(request).enqueue(new Callback() {
                    public void onFailure(Call call, IOException error) { body.completeExceptionally(error); }
                    public void onResponse(Call call, okhttp3.Response response) { try (response) { body.complete(copy(response.body().byteStream())); } catch (Throwable error) { body.completeExceptionally(error); } }
                });
                received = body.join();
            } else try (okhttp3.Response response = client.newCall(request).execute()) {
                if (args[2].equals("verify_abort")) {
                    var stream = response.body().byteStream(); int first = stream.read();
                    try { stream.read(); throw new AssertionError("Expected application exception"); }
                    catch (IOException error) { if (error != applicationFailure) throw new AssertionError("Agent replaced application exception", error); }
                    System.out.println("ABORT_ORIGINAL=true"); received = new byte[]{(byte)first};
                } else if (transportFailure || corruptGzip) {
                    var bytes = new ByteArrayOutputStream(); var stream = response.body().byteStream();
                    try { int b; while ((b = stream.read()) >= 0) bytes.write(b); throw new AssertionError("Expected native body failure"); }
                    catch (IOException expected) {
                        if (args[2].equals("verify_transport_wrapped") && !(expected.getCause() instanceof java.net.ProtocolException)) throw new AssertionError("Original native cause lost", expected);
                        if ((args[2].equals("verify_transport_independent") || args[2].equals("verify_gzip_abort")) && expected != applicationFailure) throw new AssertionError("Application exception replaced", expected);
                        System.out.println("TRANSPORT_ORIGINAL=" + expected.getClass().getName());
                        System.out.println("READ_ERROR=" + Base64.getEncoder().encodeToString((expected.getClass().getName() + ":" + expected.getMessage()).getBytes(StandardCharsets.UTF_8)));
                    }
                    received = bytes.toByteArray();
                } else received = copy(response.body().byteStream());
            } catch (IOException error) {
                if (!args[2].equals("verify_eager") || error != applicationFailure) throw error;
                System.out.println("ABORT_ORIGINAL=true"); received = new byte[0];
            }
            if (args[2].startsWith("metadata")) System.out.println("GETTER_CALLS=" + getterCalls.get());
            client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll();
        }
        System.out.println("BODY=" + Base64.getEncoder().encodeToString(received));
    }
    static void legacyOkhttp(String[] args) throws IOException {
        OkHttpClient client = new OkHttpClient.Builder().build();
        try {
            try (var response = client.newCall(new okhttp3.Request.Builder().url(args[1]).get().build()).execute()) {
                System.out.println("LEGACY_ORDINARY_STATUS=" + response.code()); response.body().bytes();
            }
            var body = RequestBody.create(MediaType.parse("application/json"), request(args[2]));
            try (var response = client.newCall(new okhttp3.Request.Builder().url(args[1]).post(body).build()).execute()) {
                System.out.println("LEGACY_AI_STATUS=" + response.code());
                System.out.println("BODY=" + Base64.getEncoder().encodeToString(response.body().bytes()));
            }
        } finally { client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll(); }
    }
    static byte[] copy(InputStream stream) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(); byte[] chunk = new byte[3];
        int n; while ((n = stream.read(chunk)) >= 0) bytes.write(chunk, 0, n); return bytes.toByteArray();
    }
}
