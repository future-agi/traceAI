package ai.traceai.weaviate;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.*;

class WeaviateReadmeConsumerTest {
    @Test void runsUnmodifiedReadmeAgainstPackagedJarWithoutLegacySdk() throws Exception {
        assertThatThrownBy(() -> Class.forName("io.weaviate.client.WeaviateClient")).isInstanceOf(ClassNotFoundException.class);
        var posts = new CopyOnWriteArrayList<ExportTraceServiceRequest>();
        var headers = new CopyOnWriteArrayList<String>();
        HttpServer otlp = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        otlp.createContext("/tracer/v1/traces", exchange -> {
            posts.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
            headers.add(exchange.getRequestHeaders().getFirst("X-Api-Key"));
            headers.add(exchange.getRequestHeaders().getFirst("X-Secret-Key"));
            headers.add(exchange.getRequestHeaders().getFirst("fi-project-name"));
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        otlp.start();
        try (var server = new WeaviateV6Fixture()) {
            var process = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-Djava.net.preferIPv4Stack=true", "-cp", System.getProperty("java.class.path"), "WeaviateExample")
                    .redirectErrorStream(true);
            var env = process.environment();
            env.put("FI_BASE_URL", "http://127.0.0.1:" + otlp.getAddress().getPort());
            env.put("FI_API_KEY", "synthetic-key");
            env.put("FI_SECRET_KEY", "synthetic-secret");
            env.put("FI_PROJECT_NAME", "weaviate-java");
            env.put("FI_HIDE_ERROR_DETAILS", "true");
            env.put("WEAVIATE_HTTP_PORT", Integer.toString(server.httpPort()));
            env.put("WEAVIATE_GRPC_PORT", Integer.toString(server.grpcPort()));
            var child = process.start();
            try {
                assertThat(child.waitFor(30, TimeUnit.SECONDS)).isTrue();
                String output = new String(child.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
                assertThat(child.exitValue()).as(output).isZero();
                assertThat(output).contains("Matches: 1");
            } finally {
                if (child.isAlive()) child.destroyForcibly();
            }
            assertThat(posts).hasSize(1);
            assertThat(headers).containsExactly("synthetic-key", "synthetic-secret", "weaviate-java");
            var spans = posts.get(0).getResourceSpansList().stream().flatMap(r -> r.getScopeSpansList().stream())
                    .flatMap(s -> s.getSpansList().stream()).toList();
            assertThat(spans).hasSize(1);
            assertThat(spans.get(0).getName()).isEqualTo("Weaviate bm25");
            assertThat(spans.get(0).toString()).doesNotContain(WeaviateV6Fixture.PRIVATE);
        } finally {
            otlp.stop(0);
        }
    }
}
