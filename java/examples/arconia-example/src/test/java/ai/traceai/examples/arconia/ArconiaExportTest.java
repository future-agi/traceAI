package ai.traceai.examples.arconia;

import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.Span;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.WebApplicationType;
import org.springframework.context.ConfigurableApplicationContext;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Boots the real example with the real application.yaml, points it at a recording stand-in collector,
 * sends requests and closes the context (which is what flushes the batch processor).
 */
class ArconiaExportTest {

    private static final String KEY = "test-api-key-123";
    private static final String SECRET = "test-secret-key-456";

    /** Runs the app with the shipped application.yaml plus overrides, makes the calls, closes, returns what the collector got. */
    private static List<OtlpRecorder.Post> run(Map<String, String> overrides, String... paths) throws Exception {
        try (OtlpRecorder collector = new OtlpRecorder()) {
            // Command-line arguments beat the real environment, so a developer's own FI_* variables cannot leak in.
            Map<String, String> props = new HashMap<>();
            props.put("server.port", "0");
            props.put("FI_BASE_URL", collector.baseUrl());
            props.put("FI_API_KEY", KEY);
            props.put("FI_SECRET_KEY", SECRET);
            props.put("FI_PROJECT_NAME", "arconia-test");
            props.putAll(overrides);
            SpringApplication app = new SpringApplication(ArconiaExampleApplication.class);
            app.setWebApplicationType(WebApplicationType.SERVLET);
            // Close fast on purpose: the batch delay is 5 s, so anything received was flushed by the close.
            String[] args = props.entrySet().stream().map(e -> "--" + e.getKey() + "=" + e.getValue()).toArray(String[]::new);
            try (ConfigurableApplicationContext ctx = app.run(args)) {
                String port = ctx.getEnvironment().getProperty("local.server.port");
                HttpClient http = HttpClient.newHttpClient();
                for (String path : paths) {
                    http.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path)).build(),
                            HttpResponse.BodyHandlers.discarding());
                }
            }
            return collector.posts();
        }
    }

    private static List<Span> spans(List<OtlpRecorder.Post> posts) throws Exception {
        List<Span> out = new ArrayList<>();
        for (OtlpRecorder.Post p : posts) {
            p.decode().getResourceSpansList().forEach(rs ->
                    rs.getScopeSpansList().forEach(ss -> out.addAll(ss.getSpansList())));
        }
        return out;
    }

    private static Map<String, String> resourceAttributes(OtlpRecorder.Post post) throws Exception {
        return post.decode().getResourceSpans(0).getResource().getAttributesList().stream()
                .collect(Collectors.toMap(KeyValue::getKey, kv -> kv.getValue().getStringValue()));
    }

    @Test
    void exportsHttpProtobufWithBothHeadersAndProjectNameOnTheResource() throws Exception {
        List<OtlpRecorder.Post> posts = run(Map.of(), "/hello?name=ada");

        assertFalse(posts.isEmpty(), "the context close must flush the span before the 5 s batch delay");
        OtlpRecorder.Post post = posts.get(0);
        assertEquals("/tracer/v1/traces", post.path());
        assertEquals("application/x-protobuf", post.headers().get("Content-Type"));
        assertEquals(KEY, post.headers().get("X-Api-Key"));
        assertEquals(SECRET, post.headers().get("X-Secret-Key"));
        assertFalse(post.headers().containsKey("Content-Encoding"), "the recipe sends uncompressed bodies (compression: none)");

        Map<String, String> resource = resourceAttributes(post);
        assertEquals("arconia-test", resource.get("project_name"));
        assertEquals("observe", resource.get("project_type"));
        assertEquals("arconia-example", resource.get("service.name"));

        // One request, one trace, one root.
        List<Span> spans = spans(posts);
        assertEquals(1, spans.stream().map(s -> s.getTraceId()).distinct().count());
        long roots = spans.stream().filter(s -> s.getParentSpanId().isEmpty()).count();
        assertEquals(1, roots, "exactly one root span");
        assertEquals(Span.SpanKind.SPAN_KIND_SERVER, spans.get(0).getKind());
    }

    @Test
    void failedRequestIsExportedAsAnErrorSpan() throws Exception {
        List<Span> spans = spans(run(Map.of(), "/fail?reason=boom"));
        assertEquals(1, spans.size());
        assertEquals(io.opentelemetry.proto.trace.v1.Status.StatusCode.STATUS_CODE_ERROR, spans.get(0).getStatus().getCode());
    }

    @Test
    void tracesDisabledExportsNothing() throws Exception {
        assertTrue(run(Map.of("arconia.otel.traces.enabled", "false"), "/hello").isEmpty());
    }

    @Test
    void exporterTypeNoneExportsNothing() throws Exception {
        assertTrue(run(Map.of("arconia.otel.traces.exporter.type", "none"), "/hello").isEmpty());
    }

    @Test
    void arconiaDefaultCompressionIsGzipSoTheRecipeSetsNoneExplicitly() throws Exception {
        // Remove the override by blanking it; Arconia falls back to its own default.
        List<OtlpRecorder.Post> posts = run(Map.of("arconia.otel.traces.exporter.otlp.compression", "gzip"), "/hello");
        assertEquals("gzip", posts.get(0).headers().get("Content-Encoding"));
    }

    @Test
    void documentedProtocolValueHttpProtobufSilentlyDisablesTheExporterOnThisArconiaVersion() throws Exception {
        // Arconia 0.30.0 and 0.31.0 document "http-protobuf" but only register the HTTP exporter for "http_protobuf".
        // If this test starts failing, upstream fixed it and the page can drop the warning.
        assertTrue(run(Map.of("arconia.otel.traces.exporter.otlp.protocol", "http-protobuf"), "/hello").isEmpty());
        assertFalse(run(Map.of("arconia.otel.traces.exporter.otlp.protocol", "http_protobuf"), "/hello").isEmpty());
    }

    @Test
    void shippedYamlContainsPlaceholdersOnly() throws Exception {
        String yaml = Files.readString(Path.of("src/main/resources/application.yaml"), StandardCharsets.UTF_8);
        assertTrue(yaml.contains("${FI_API_KEY}") && yaml.contains("${FI_SECRET_KEY}"));
        assertFalse(yaml.matches("(?s).*(X-Api-Key|X-Secret-Key):\\s*[A-Za-z0-9]{16,}.*"), "no literal key in the yaml");
    }

    @Test
    void exportedSpansReachOnlyTheTraceRoute() throws Exception {
        Set<String> paths = run(Map.of(), "/hello").stream().map(OtlpRecorder.Post::path).collect(Collectors.toSet());
        assertEquals(Set.of("/tracer/v1/traces"), paths, "no metrics or logs routes");
        assertNotNull(paths);
    }

    private static String allBytes(List<OtlpRecorder.Post> posts) {
        StringBuilder sb = new StringBuilder();
        posts.forEach(p -> sb.append(new String(p.body(), StandardCharsets.ISO_8859_1)));
        return sb.toString();
    }

    @Test
    void errorTextAndStackTraceStayOutByDefaultButTheErrorSpanAndTypeRemain() throws Exception {
        List<OtlpRecorder.Post> posts = run(Map.of(), "/hello?name=QUERYMARK1", "/fail?reason=ERRMARK2");
        String wire = allBytes(posts);
        assertFalse(wire.contains("QUERYMARK1"), "query string is not exported");
        assertFalse(wire.contains("ERRMARK2"), "exception message is not exported");
        assertFalse(wire.contains("ArconiaExampleApplication.fail"), "stack trace is not exported");
        assertFalse(wire.contains("DispatcherServlet"), "stack trace is not exported");
        Span failed = spans(posts).stream().filter(s -> s.getStatus().getCode()
                == io.opentelemetry.proto.trace.v1.Status.StatusCode.STATUS_CODE_ERROR).findFirst().orElseThrow();
        assertEquals("", failed.getStatus().getMessage());
        var event = failed.getEvents(0);
        assertEquals("exception", event.getName());
        assertTrue(event.getAttributesList().stream().anyMatch(kv -> kv.getKey().equals("exception.type")
                && kv.getValue().getStringValue().equals("java.lang.IllegalStateException")));
    }

    @Test
    void optInExportsTheRawErrorDetails() throws Exception {
        String wire = allBytes(run(Map.of("fi.export-error-details", "true"), "/fail?reason=ERRMARK2"));
        assertTrue(wire.contains("ERRMARK2"));
        assertTrue(wire.contains("exception.stacktrace"));
    }

    @Test
    void noHostnameUserNameOrPathInTheResource() throws Exception {
        List<OtlpRecorder.Post> posts = run(Map.of(), "/hello");
        String wire = allBytes(posts);
        assertFalse(wire.contains(System.getProperty("user.name")), "no OS user name");
        assertFalse(wire.contains(System.getProperty("user.home")), "no home path");
        assertFalse(wire.contains(System.getProperty("user.dir")), "no working directory");
        assertFalse(resourceAttributes(posts.get(0)).containsKey("host.name"));
        assertFalse(resourceAttributes(posts.get(0)).containsKey("process.command_line"));
    }
}
