package ai.traceai.auto;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import java.net.URI;
import java.net.http.*;
import java.util.*;
import okhttp3.*;

/** Exercises already-created clients and an application-owned, unshaded provider. */
public final class ReconfigurationApp {
    static void release(String endpoint) throws Exception {
        var request = HttpRequest.newBuilder(URI.create(endpoint).resolve("/release")).GET().build();
        HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.discarding());
    }
    static void run(String[] args) throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        Class<?> agent = Class.forName("ai.traceai.auto.TraceAIAuto");
        Object initialProvider = agent.getMethod("init").invoke(null);
        if (initialProvider == null || initialProvider != agent.getMethod("init", Map.class).invoke(null, Map.of("base_url", "http://127.0.0.1:1", "project_name", "ignored"))) throw new AssertionError("init replaced its provider");
        HttpClient jdk = args[0].equals("jdk") ? HttpClient.newHttpClient() : null;
        OkHttpClient okhttp = args[0].equals("okhttp") ? new OkHttpClient() : null;
        try {
            if (args[2].equals("reconfigure_inflight")) {
                for (int i = 0; i < 2; i++) {
                    byte[] received;
                    if (jdk != null) {
                        var request = HttpRequest.newBuilder(URI.create(args[1])).header("content-type", "application/json").POST(HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
                        var response = jdk.send(request, HttpResponse.BodyHandlers.ofInputStream());
                        agent.getMethod("init", Map.class).invoke(null, Map.of("project_name", "during-stream-" + i)); release(args[1]);
                        try (var stream = response.body()) { received = stream.readAllBytes(); }
                    } else {
                        var request = new okhttp3.Request.Builder().url(args[1]).post(RequestBody.create(MediaType.get("application/json"), PlainApp.REQUEST)).build();
                        try (var response = okhttp.newCall(request).execute()) {
                            agent.getMethod("init", Map.class).invoke(null, Map.of("project_name", "during-stream-" + i)); release(args[1]); received = response.body().bytes();
                        }
                    }
                    System.out.println("BODY=" + Base64.getEncoder().encodeToString(received)); agent.getMethod("flush").invoke(null);
                }
                agent.getMethod("shutdown").invoke(null); return;
            }
            for (int i = 0; i < 2; i++) {
                if (i == 1) {
                    agent.getMethod("flush").invoke(null);
                    Map<String, Object> options = args[2].endsWith("transport") ? Map.of("disabled_instrumentations", args[0].equals("jdk") ? "java.net.http" : "okhttp") : args[2].endsWith("wire") ? Map.of("wire_capture", false) : Map.of();
                    for (int repeat = 0; repeat < 3; repeat++) agent.getMethod("init", Map.class).invoke(null, options);
                }
                byte[] received;
                if (jdk != null) {
                    var request = java.net.http.HttpRequest.newBuilder(URI.create(args[1])).header("content-type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
                    received = jdk.send(request, java.net.http.HttpResponse.BodyHandlers.ofByteArray()).body();
                } else {
                    var request = new okhttp3.Request.Builder().url(args[1]).post(RequestBody.create(MediaType.get("application/json"), PlainApp.REQUEST)).build();
                    try (var response = okhttp.newCall(request).execute()) { received = response.body().bytes(); }
                }
                System.out.println("BODY=" + Base64.getEncoder().encodeToString(received));
            }
            agent.getMethod("shutdown").invoke(null);
            if (initialProvider != agent.getMethod("init").invoke(null)) throw new AssertionError("init restarted after shutdown");
            GlobalOpenTelemetry.getTracer("app").spanBuilder("after-agent-shutdown").startSpan().end();
            if (exporter.getFinishedSpanItems().size() != 1) throw new AssertionError("Agent shut down the application's provider");
            System.out.println("APP_PROVIDER_ALIVE=true");
        } finally {
            provider.shutdown();
            if (okhttp != null) { okhttp.dispatcher().executorService().shutdown(); okhttp.connectionPool().evictAll(); }
        }
    }
}
