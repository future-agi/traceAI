package ai.traceai.auto;

import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import okhttp3.*;

/** A real shaded-agent probe: suppressed transport credentials must protect an unrelated thread. */
public final class RegistryApp {
    static final String HEADER = "ROUND5_TRANSPORT_ONLY_HEADER", QUERY = "ROUND5_TRANSPORT_ONLY_QUERY", API = "ROUND5_PUBLIC_REGISTRATION", ENV = "ROUND5_ENVIRONMENT_CREDENTIAL";
    static final String PROXY_PAYLOAD = Base64.getEncoder().encodeToString("probe:proxy-secret-802701xx".getBytes(StandardCharsets.UTF_8));
    static final String PROXY = "Basic " + PROXY_PAYLOAD;
    static final String SESSION = "ROUND5_AWS_SESSION_TOKEN_81973264", BODY_SECRET = "ROUND5_TRAILING_PASSWORD_81973264";
    static final String IMAGE = "https://image.example/PRIVATE_IMAGE";
    static final String MESSAGES = "[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"PUBLIC_MESSAGE_TEXT\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"" + IMAGE + "\"}},{\"type\":\"input_audio\",\"input_audio\":{\"data\":\"QUJD\",\"format\":\"wav\"}}]}]";
    static void run(String[] args) throws Exception {
        boolean proxy = args[2].equals("registry_proxy"), media = args[2].equals("registry_media");
        boolean authToken = args[2].equals("registry_auth");
        boolean sessionToken = args[2].equals("registry_session") || authToken, observeBody = args[2].startsWith("registry_body");
        var memory = io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter.create();
        var provider = SdkTracerProvider.builder().addSpanProcessor(io.opentelemetry.sdk.trace.export.SimpleSpanProcessor.create(memory)).build();
        var sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        var tracer = sdk.getTracer("registry-application");
        Span library = tracer.spanBuilder("registry-library").startSpan(); library.setAttribute("gen_ai.span.kind", observeBody ? "CHAIN" : "LLM");
        if (media) {
            library.setAttribute("gen_ai.input.messages", MESSAGES);
            library.setAttribute(io.opentelemetry.api.common.AttributeKey.stringArrayKey("gen_ai.input.messages.array"), java.util.List.of(MESSAGES, "PUBLIC_ARRAY_TEXT"));
            library.setAttribute("input.value", MESSAGES);
            library.setAttribute("gen_ai.input.messages.0.content.0.type", "image_url");
            library.setAttribute("gen_ai.input.messages.0.content.0.url", IMAGE);
            library.setAttribute("gen_ai.input.messages.0.content.1.type", "text");
            library.setAttribute("gen_ai.input.messages.0.content.1.text", "PUBLIC_FLAT_TEXT");
        }
        String endpoint = proxy ? "http://proxy-target.invalid/v1/chat/completions" : args[1];
        var proxyAddress = proxy ? new java.net.InetSocketAddress("127.0.0.1", Integer.parseInt(System.getenv("TRACEAI_TEST_PROXY_PORT"))) : null;
        String header = proxy ? "Proxy-Authorization" : authToken ? "X-Auth-Token" : sessionToken ? "X-Amz-Security-Token" : "Authorization";
        String value = proxy ? PROXY : sessionToken ? SESSION : "Bearer " + HEADER;
        String outgoing = PlainApp.request(args[2]);
        AtomicInteger writes = new AtomicInteger(); byte[] received;
        try (var scope = library.makeCurrent()) {
            Span nativeSpan = tracer.spanBuilder("registry-native").startSpan();
            try (var nativeScope = nativeSpan.makeCurrent()) {
                if (args[0].equals("jdk")) {
                    var raw = HttpRequest.BodyPublishers.ofString(outgoing);
                    var body = new HttpRequest.BodyPublisher() {
                        public long contentLength() { return raw.contentLength(); }
                        public void subscribe(java.util.concurrent.Flow.Subscriber<? super java.nio.ByteBuffer> subscriber) { writes.incrementAndGet(); raw.subscribe(subscriber); }
                    };
                    var request = HttpRequest.newBuilder(URI.create(endpoint + "?api_key=" + QUERY)).header(header, value).header("Content-Type", "application/json").POST(body).build();
                    var builder = HttpClient.newBuilder(); if (proxy) builder.proxy(java.net.ProxySelector.of(proxyAddress));
                    received = builder.build().sendAsync(request, HttpResponse.BodyHandlers.ofByteArray()).get(3, java.util.concurrent.TimeUnit.SECONDS).body();
                } else {
                    var builder = new OkHttpClient.Builder().addNetworkInterceptor(chain -> chain.proceed(chain.request().newBuilder().header(proxy || sessionToken ? header : "x-api-key", proxy || sessionToken ? value : HEADER).url(chain.request().url().newBuilder().setQueryParameter("api_key", QUERY).build()).build()));
                    if (proxy) builder.proxy(new java.net.Proxy(java.net.Proxy.Type.HTTP, proxyAddress));
                    var client = builder.build();
                    try {
                        var body = new RequestBody() {
                            public MediaType contentType() { return MediaType.get("application/json"); }
                            public void writeTo(okio.BufferedSink sink) throws java.io.IOException { writes.incrementAndGet(); sink.writeUtf8(outgoing); }
                        };
                        var future = new CompletableFuture<byte[]>();
                        client.newCall(new Request.Builder().url(endpoint).post(body).build()).enqueue(new Callback() {
                            public void onFailure(Call call, java.io.IOException error) { future.completeExceptionally(error); }
                            public void onResponse(Call call, Response response) { try (response) { future.complete(response.body().bytes()); } catch (Throwable error) { future.completeExceptionally(error); } }
                        });
                        received = future.get(3, java.util.concurrent.TimeUnit.SECONDS);
                    } finally { client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll(); }
                }
            } finally { nativeSpan.end(); }
        } finally { library.end(); }
        if (media) {
            if (!MESSAGES.equals(((io.opentelemetry.sdk.trace.ReadableSpan)library).toSpanData().getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("gen_ai.input.messages")))) throw new AssertionError("Application messages changed");
            System.out.println("MEDIA_UNCHANGED=true");
        }
        try { Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("registerSecret", String.class).invoke(null, API); } catch (ClassNotFoundException ignored) { }
        CompletableFuture.runAsync(() -> {
            String credential = proxy ? PROXY : sessionToken ? SESSION : observeBody ? BODY_SECRET : HEADER;
            var context = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.builder().put("vendor", credential).build());
            var attrs = io.opentelemetry.api.common.Attributes.builder().put("echo", credential).put("key-" + credential, credential)
                .put(io.opentelemetry.api.common.AttributeKey.stringArrayKey("echo.array"), java.util.List.of(credential, proxy ? PROXY_PAYLOAD : HEADER))
                .put("http.request.header." + header.toLowerCase(java.util.Locale.ROOT), credential)
                .put("exception.message", credential).put("exception.stacktrace", credential).put("public", "PUBLIC_BODY_CONTROL").build();
            Span unrelated = sdk.tracerBuilder("registry-" + credential).setInstrumentationVersion(credential).setSchemaUrl("https://scope/" + credential).build()
                .spanBuilder(sessionToken ? "registry-unrelated-" + credential : "registry-unrelated").setNoParent().addLink(context, attrs).setAllAttributes(attrs).startSpan();
            String echoed = observeBody ? BODY_SECRET : sessionToken ? SESSION : proxy ? String.join(" ", PROXY, PROXY_PAYLOAD, QUERY, API, ENV) : String.join(" ", HEADER, QUERY, API, ENV);
            unrelated.setAttribute("echo", echoed); unrelated.setStatus(StatusCode.ERROR, echoed);
            unrelated.addEvent("echo-" + credential, attrs);
            unrelated.end();
        }).join();
        if (sessionToken || observeBody) {
            String credential = sessionToken ? SESSION : BODY_SECRET;
            var original = memory.getFinishedSpanItems().stream().filter(span -> span.getName().startsWith("registry-unrelated")).findFirst().orElseThrow();
            if (!credential.equals(original.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("echo"))) || !credential.equals(original.getStatus().getDescription())) throw new AssertionError("Application credential telemetry changed");
            System.out.println("APP_CREDENTIALS_UNCHANGED=true");
        }
        provider.shutdown();
        System.out.println("REQUEST_WRITES=" + writes.get());
        if (writes.get() != 1) throw new AssertionError("Suppressed request was buffered/replayed");
        System.out.println("BODY=" + Base64.getEncoder().encodeToString(received));
    }
}
