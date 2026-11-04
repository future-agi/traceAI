package ai.traceai.pgvector;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class TracedPgVectorStoreOtlpTest {
    @Test
    void exportsOneChildSpanPerSearchAndPrivateRuntimeErrors() throws Exception {
        List<ExportTraceServiceRequest> requests = new CopyOnWriteArrayList<>();
        List<String> paths = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            paths.add(exchange.getRequestURI().getPath());
            requests.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody()));
            exchange.getResponseHeaders().add("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(
                OtlpHttpSpanExporter.builder()
                    .setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces")
                    .setTimeout(Duration.ofSeconds(5)).build())).build()) {
            FITracer tracer = new FITracer(provider.get("pgvector-wire-test"),
                TraceConfig.builder().hideErrorDetails(true).build());
            Connection connection = mock(Connection.class);
            PreparedStatement statement = mock(PreparedStatement.class);
            ResultSet rows = mock(ResultSet.class);
            when(connection.prepareStatement(anyString())).thenReturn(statement);
            when(statement.executeQuery()).thenReturn(rows);
            when(rows.next()).thenReturn(true, false);
            when(rows.getString("embedding")).thenReturn("[1,2,3]");
            when(rows.getString("metadata")).thenReturn("{\"text\":\"private-document\"}");
            TracedPgVectorStore store = new TracedPgVectorStore(connection, tracer);
            Span parent = provider.get("pgvector-wire-test").spanBuilder("request").startSpan();
            try (Scope scope = parent.makeCurrent()) {
                assertThat(store.search("documents", new float[]{1, 2, 3}, 3, "l2")).hasSize(1);
                IllegalStateException failure = new IllegalStateException("private-error-details");
                when(statement.executeQuery()).thenThrow(failure);
                assertThatThrownBy(() -> store.search("documents", new float[]{1, 2, 3}, 3, "l2"))
                    .isSameAs(failure);
            } finally {
                parent.end();
            }
            var flush = provider.forceFlush().join(5, TimeUnit.SECONDS);
            assertThat(flush.isSuccess()).isTrue();
            var spans = requests.stream().flatMap(request -> request.getResourceSpansList().stream())
                .flatMap(resource -> resource.getScopeSpansList().stream())
                .flatMap(scope -> scope.getSpansList().stream()).collect(Collectors.toList());
            assertThat(paths).isNotEmpty().containsOnly("/v1/traces");
            assertThat(spans).hasSize(3);
            var searches = spans.stream().filter(span -> span.getName().equals("PgVector Search"))
                .collect(Collectors.toList());
            assertThat(searches).hasSize(2).allSatisfy(span -> {
                assertThat(span.getTraceId().toByteArray()).isEqualTo(parent.getSpanContext().getTraceIdBytes());
                assertThat(span.getParentSpanId().toByteArray()).isEqualTo(parent.getSpanContext().getSpanIdBytes());
                Map<String, AnyValue> attributes = attributes(span);
                assertThat(attributes.get("fi.span.kind").getStringValue()).isEqualTo("RETRIEVER");
                assertThat(attributes.get("db.system").getStringValue()).isEqualTo("pgvector");
            });
            var success = searches.stream().filter(span -> span.getStatus().getCode() == Status.StatusCode.STATUS_CODE_OK)
                .findFirst().orElseThrow();
            assertThat(attributes(success).get("db.vector.results.count").getIntValue()).isEqualTo(1);
            var failure = searches.stream().filter(span -> span.getStatus().getCode() == Status.StatusCode.STATUS_CODE_ERROR)
                .findFirst().orElseThrow();
            assertThat(failure.getStatus().getMessage()).isEmpty();
            assertThat(attributes(failure).get("error.type").getStringValue()).isEqualTo(IllegalStateException.class.getName());
            assertThat(failure.getEventsList()).singleElement().satisfies(event -> {
                assertThat(event.getName()).isEqualTo("exception");
                assertThat(event.getAttributesList()).extracting(KeyValue::getKey).containsExactly("exception.type");
            });
            assertThat(requests.toString()).doesNotContain("private-document", "private-error-details", "[1,2,3]");
        } finally {
            server.stop(0);
        }
    }

    private static Map<String, AnyValue> attributes(io.opentelemetry.proto.trace.v1.Span span) {
        return span.getAttributesList().stream().collect(Collectors.toMap(KeyValue::getKey, KeyValue::getValue));
    }
}
