package ai.traceai.auto;

import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.atomic.AtomicInteger;
import okhttp3.*;

/** The exact 31-byte PNG padding regression, exercised through the packaged agent. */
public final class MagicMediaApp {
    static final String MEDIA = "iVBORw0KGgoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==";
    static final String REQUEST = "{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"" + MEDIA + "\"}]}";

    static void run(String[] args) throws Exception {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
            String endpoint = args[1] + "?public=keep&sample=" + URLEncoder.encode(MEDIA, StandardCharsets.UTF_8);
            var original = Attributes.builder().put("probe", MEDIA).put("probe.json", "{\"nested\":[{\"content\":\"" + MEDIA + "\"}]}")
                .put("http.response.url", endpoint).put("public", "PUBLIC_MAGIC_CONTROL").build();
            var span = sdk.getTracer("magic-media-application").spanBuilder("magic-media-application").setAllAttributes(original).startSpan();
            AtomicInteger writes = new AtomicInteger(); byte[] received;
            try (var scope = span.makeCurrent()) {
                if (args[0].equals("jdk")) {
                    var raw = HttpRequest.BodyPublishers.ofString(REQUEST);
                    var body = new HttpRequest.BodyPublisher() {
                        public long contentLength() { return raw.contentLength(); }
                        public void subscribe(java.util.concurrent.Flow.Subscriber<? super java.nio.ByteBuffer> subscriber) { writes.incrementAndGet(); raw.subscribe(subscriber); }
                    };
                    var request = HttpRequest.newBuilder(URI.create(endpoint)).header("Content-Type", "application/json").POST(body).build();
                    var response = HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofByteArray());
                    if (response.statusCode() != 200 || !response.uri().toString().equals(endpoint)) throw new AssertionError("Application response changed");
                    received = response.body();
                } else {
                    var client = new OkHttpClient();
                    try {
                        var body = new RequestBody() {
                            public MediaType contentType() { return MediaType.get("application/json"); }
                            public void writeTo(okio.BufferedSink sink) throws java.io.IOException { writes.incrementAndGet(); sink.writeUtf8(REQUEST); }
                        };
                        try (var response = client.newCall(new Request.Builder().url(endpoint).post(body).build()).execute()) {
                            if (response.code() != 200 || !response.request().url().toString().equals(endpoint)) throw new AssertionError("Application response changed");
                            received = response.body().bytes();
                        }
                    } finally { client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll(); }
                }
            } finally { span.end(); }
            var snapshot = memory.getFinishedSpanItems().get(0);
            if (!original.asMap().equals(snapshot.getAttributes().asMap())) throw new AssertionError("Application span changed");
            if (writes.get() != 1) throw new AssertionError("Request buffered/replayed");
            System.out.println("MEDIA_UNCHANGED=true");
            System.out.println("REQUEST_WRITES=" + writes.get());
            System.out.println("BODY=" + Base64.getEncoder().encodeToString(received));
        }
    }
}
