package ai.traceai.milvus;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.milvus.orm.iterator.QueryIterator;
import io.milvus.v2.client.MilvusClientV2;
import io.milvus.v2.exception.ErrorCode;
import io.milvus.v2.exception.MilvusClientException;
import io.milvus.v2.service.vector.request.HybridSearchReq;
import io.milvus.v2.service.vector.request.QueryIteratorReq;
import io.milvus.v2.service.vector.request.QueryReq;
import io.milvus.v2.service.vector.request.SearchReq;
import io.milvus.v2.service.vector.response.SearchResp;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class MilvusOtlpTest {
    @Test
    void exportsParentedSemanticSpansAndRedactsSensitivePayloadsOnTheWire() throws Exception {
        List<byte[]> bodies = new CopyOnWriteArrayList<>();
        List<String> paths = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            paths.add(exchange.getRequestURI().getPath());
            bodies.add(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().add("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        try (var exporter = OtlpHttpSpanExporter.builder()
                .setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces").build();
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracer = new FITracer(provider.get("milvus-wire"), TraceConfig.builder()
                    .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
            var client = mock(MilvusClientV2.class);
            var response = TracedMilvusClientTest.searchResponse();
            when(client.hybridSearch(any())).thenReturn(response);
            var iterator = mock(QueryIterator.class);
            when(iterator.next()).thenReturn(List.of());
            when(client.queryIterator(any())).thenReturn(iterator);
            var failure = new MilvusClientException(ErrorCode.SERVER_ERROR, "private-error-message");
            when(client.query(any())).thenThrow(failure);
            var traced = new TracedMilvusClient(client, tracer);
            Span parent = provider.get("test").spanBuilder("application").startSpan();
            int expected = 5;
            try (Scope ignored = parent.makeCurrent()) {
                assertThat(traced.hybridSearch(HybridSearchReq.builder().collectionName("books").topK(2).build()))
                        .isSameAs(response);
                try (var pages = traced.queryIterator(QueryIteratorReq.builder().collectionName("books")
                        .expr("private-filter").build())) {
                    assertThat(pages.next()).isEmpty();
                }
                assertThatThrownBy(() -> traced.query(QueryReq.builder().collectionName("books")
                        .filter("private-filter").build())).isSameAs(failure);
                try {
                    var method = MilvusClientV2.class.getMethod("searchAsync", SearchReq.class);
                    var future = new CompletableFuture<SearchResp>();
                    when(method.invoke(client, any(SearchReq.class))).thenReturn(future);
                    assertThat(traced.searchAsync(SearchReq.builder().collectionName("books")
                            .filter("private-filter").build())).isSameAs(future);
                    future.complete(response);
                    expected++;
                } catch (NoSuchMethodException floorSdk) {
                    // SDK 2.6.12 has no native async methods; the unit tests cover its explicit guard.
                }
            }
            assertThat(provider.forceFlush().join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
            List<io.opentelemetry.proto.trace.v1.Span> spans = new ArrayList<>();
            for (byte[] body : bodies) {
                assertThat(new String(body, StandardCharsets.UTF_8))
                        .doesNotContain("private-filter", "private-document", "private-id", "private-error-message");
                for (var resource : ExportTraceServiceRequest.parseFrom(body).getResourceSpansList()) {
                    for (var scope : resource.getScopeSpansList()) spans.addAll(scope.getSpansList());
                }
            }
            assertThat(spans).hasSize(expected);
            assertThat(paths).allMatch("/v1/traces"::equals);
            for (var span : spans) {
                assertThat(HexFormat.of().formatHex(span.getParentSpanId().toByteArray()))
                        .isEqualTo(parent.getSpanContext().getSpanId());
                assertThat(span.getEndTimeUnixNano()).isGreaterThanOrEqualTo(span.getStartTimeUnixNano());
                assertThat(span.getAttributesList()).anySatisfy(attr -> {
                    assertThat(attr.getKey()).isEqualTo("db.system");
                    assertThat(attr.getValue().getStringValue()).isEqualTo("milvus");
                });
                assertThat(span.getAttributesList()).anySatisfy(attr -> {
                    assertThat(attr.getKey()).isEqualTo("fi.span.kind");
                    assertThat(attr.getValue().getStringValue()).isEqualTo("RETRIEVER");
                });
                assertThat(span.getAttributesList()).anySatisfy(attr ->
                        assertThat(attr.getKey()).isEqualTo("db.operation.name"));
            }
            var error = spans.stream().filter(span -> span.getName().equals("Milvus Query")).findFirst().orElseThrow();
            assertThat(error.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
            assertThat(error.getStatus().getMessage()).isEmpty();
            assertThat(error.getEventsList()).hasSize(1);
            assertThat(error.getEvents(0).getAttributesList()).hasSize(1);
            assertThat(error.getEvents(0).getAttributes(0).getKey()).isEqualTo("exception.type");
            parent.end();
        } finally {
            server.stop(0);
        }
    }
}
