package ai.traceai.weaviate;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.weaviate.client.Config;
import io.weaviate.client.WeaviateClient;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.*;

class WeaviateLegacyConsumerTest {
    @Test void retainsLegacyEntryPointWithoutV6Sdk() throws Exception {
        assertThatThrownBy(() -> Class.forName("io.weaviate.client6.v1.api.WeaviateClient")).isInstanceOf(ClassNotFoundException.class);
        HttpServer http = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        http.createContext("/", exchange -> {
            byte[] response = "{\"id\":\"00000000-0000-0000-0000-000000000001\",\"class\":\"Article\",\"properties\":{\"title\":\"legacy\"}}"
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        http.start();
        var exporter = InMemorySpanExporter.create();
        try (var sdk = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var client = new WeaviateClient(new Config("http", "127.0.0.1:" + http.getAddress().getPort()));
            var traced = new TracedWeaviateClient(client, new FITracer(sdk.get("legacy"), TraceConfig.builder().build()));
            var result = traced.getObject("Article", "00000000-0000-0000-0000-000000000001");
            assertThat(result.hasErrors()).isFalse();
            assertThat(result.getResult()).hasSize(1);
            assertThat(traced.unwrap()).isSameAs(client);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        } finally {
            http.stop(0);
        }
    }
}
