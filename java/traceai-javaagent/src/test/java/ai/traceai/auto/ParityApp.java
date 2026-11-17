package ai.traceai.auto;

import java.net.URI;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.Flow;
import okhttp3.*;
import okio.*;

/** Runs unchanged with wire disabled and enabled; emits only deterministic application observations. */
public final class ParityApp {
    static void run(String[] args) throws Exception {
        String scenario = args[2]; byte[] bytes;
        if (args[0].equals("jdk")) {
            var client = HttpClient.newHttpClient();
            var request = HttpRequest.newBuilder(URI.create(args[1])).header("content-type", "application/json").POST(HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
            switch (scenario) {
                case "parity_string": bytes = client.send(request, HttpResponse.BodyHandlers.ofString()).body().getBytes(StandardCharsets.UTF_8); break;
                case "parity_bytes": bytes = client.sendAsync(request, HttpResponse.BodyHandlers.ofByteArray()).join().body(); break;
                case "parity_mapping": bytes = client.send(request, info -> HttpResponse.BodySubscribers.mapping(HttpResponse.BodySubscribers.ofByteArray(), value -> {
                    System.out.println("PARSE_START=" + System.currentTimeMillis());
                    try { Thread.sleep(250); } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                    return value;
                })).body(); break;
                case "parity_file": {
                    Path file = Files.createTempFile(Path.of("target"), "parity-", ".json");
                    try { Path body = client.send(request, HttpResponse.BodyHandlers.ofFile(file)).body(); if (!body.equals(file)) throw new AssertionError("file result replaced"); bytes = Files.readAllBytes(file); }
                    finally { Files.deleteIfExists(file); } break;
                }
                case "parity_lines": {
                    try (var lines = client.send(request, HttpResponse.BodyHandlers.ofLines()).body()) { bytes = String.join("\n", lines.toList()).getBytes(StandardCharsets.UTF_8); } break;
                }
                case "parity_publisher": {
                    var publisher = client.send(request, HttpResponse.BodyHandlers.ofPublisher()).body();
                    var output = new java.io.ByteArrayOutputStream(); var done = new CompletableFuture<byte[]>();
                    publisher.subscribe(new Flow.Subscriber<>() {
                        Flow.Subscription subscription;
                        public void onSubscribe(Flow.Subscription value) { subscription = value; value.request(1); }
                        public void onNext(List<ByteBuffer> buffers) { for (var buffer : buffers) { byte[] chunk = new byte[buffer.remaining()]; buffer.get(chunk); output.writeBytes(chunk); } subscription.request(1); }
                        public void onError(Throwable error) { done.completeExceptionally(error); }
                        public void onComplete() { done.complete(output.toByteArray()); }
                    }); bytes = done.get(5, TimeUnit.SECONDS); break;
                }
                default: {
                    var response = client.send(request, HttpResponse.BodyHandlers.ofInputStream());
                    if (scenario.equals("parity_unread")) { System.out.println("PARITY=unread"); response.body().close(); bytes = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8); }
                    else try (var stream = response.body()) { bytes = stream.readAllBytes(); }
                }
            }
        } else {
            var original = new java.util.concurrent.atomic.AtomicReference<Response>();
            var originalBody = new java.util.concurrent.atomic.AtomicReference<SpecialBody>();
            var builder = new OkHttpClient.Builder().addNetworkInterceptor(chain -> {
                var response = chain.proceed(chain.request());
                var special = new SpecialBody(response.body()); originalBody.set(special);
                return response.newBuilder().body(special).build();
            });
            builder.build(); // Put the recorder inside the agent's application interceptor, if installed.
            builder.addInterceptor(chain -> { var response = chain.proceed(chain.request()); original.set(response); return response; });
            var client = builder.build();
            try (var response = client.newCall(new Request.Builder().url(args[1]).post(RequestBody.create(MediaType.get("application/json"), PlainApp.REQUEST)).build()).execute()) {
                if (response != original.get() || response.body() != originalBody.get()) throw new AssertionError("response/body identity replaced");
                SpecialBody body = (SpecialBody)response.body();
                if (!body.helper().equals("subclass-helper") || body.sourceCalls != 0) throw new AssertionError("body touched eagerly");
                if (scenario.equals("parity_unread")) { System.out.println("PARITY=unread"); bytes = AgentE2ETest.BASIC.getBytes(StandardCharsets.UTF_8); }
                else {
                    var source = body.source(); if (source != body.source()) throw new AssertionError("source identity replaced");
                    if (scenario.equals("parity_string")) bytes = body.string().getBytes(StandardCharsets.UTF_8);
                    else if (scenario.equals("parity_mapping")) { bytes = body.bytes(); System.out.println("PARSE_START=" + System.currentTimeMillis()); Thread.sleep(250); }
                    else bytes = body.byteStream().readAllBytes();
                }
                System.out.println("PARITY=identity,helper,lazy,source");
            } finally { client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll(); }
        }
        System.out.println("BODY=" + Base64.getEncoder().encodeToString(bytes));
    }
    static final class SpecialBody extends ResponseBody {
        final ResponseBody delegate; int sourceCalls;
        SpecialBody(ResponseBody delegate) { this.delegate = delegate; }
        String helper() { return "subclass-helper"; }
        public MediaType contentType() { return delegate.contentType(); }
        public long contentLength() { return delegate.contentLength(); }
        public BufferedSource source() { sourceCalls++; return delegate.source(); }
    }
}
