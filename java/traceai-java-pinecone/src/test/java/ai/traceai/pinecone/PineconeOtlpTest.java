package ai.traceai.pinecone;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceResponse;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.pinecone.clients.Index;
import io.pinecone.proto.FetchResponse;
import io.pinecone.proto.QueryResponse;
import io.pinecone.proto.ScoredVector;
import io.pinecone.proto.UpsertResponse;
import io.pinecone.proto.Vector;
import io.pinecone.unsigned_indices_model.QueryResponseWithUnsignedIndices;
import io.pinecone.unsigned_indices_model.VectorWithUnsignedIndices;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class PineconeOtlpTest {
    @Test
    void exportsOneParentedSpanPerOperationWithoutFilterVectorIdOrErrorPayloads() throws Exception {
        var requests = new LinkedBlockingQueue<ExportTraceServiceRequest>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            try {
                requests.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody()));
                byte[] response = ExportTraceServiceResponse.getDefaultInstance().toByteArray();
                exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
                exchange.sendResponseHeaders(200, response.length);
                exchange.getResponseBody().write(response);
            } finally {
                exchange.close();
            }
        });
        server.start();
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(OtlpHttpSpanExporter.builder()
                        .setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces")
                        .build())).build()) {
            Index index = mock(Index.class);
            var match = ScoredVector.newBuilder().setId("private-vector-id").setScore(0.75f)
                    .addValues(0.123456f).build();
            var query = new QueryResponseWithUnsignedIndices(QueryResponse.newBuilder().addMatches(match).build());
            when(index.query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean()))
                    .thenReturn(query);
            when(index.fetch(any(), any())).thenReturn(FetchResponse.newBuilder()
                    .putVectors("private-vector-id", Vector.newBuilder().setId("private-vector-id").build()).build());
            when(index.upsert(anyList(), any())).thenReturn(UpsertResponse.newBuilder().setUpsertedCount(1).build());
            var traced = new TracedPineconeIndex(index, new FITracer(provider.get("wire-test"),
                    TraceConfig.builder().hideInputs(true).hideOutputs(true).hideErrorDetails(true).build()), "index");
            Span parent = provider.get("wire-test").spanBuilder("parent").startSpan();
            try (Scope ignored = parent.makeCurrent()) {
                assertThat(traced.query(List.of(0.123456f), 3, "ns",
                        Map.of("private-filter", Map.of("$eq", "private-email@example.invalid")), false, false))
                        .isSameAs(query);
                traced.fetchResponse(List.of("private-vector-id"), "ns");
                traced.upsertResponse(List.of(new VectorWithUnsignedIndices("private-vector-id", List.of(0.123456f))), "ns");
                traced.deleteByIds(List.of("private-vector-id"), "ns");
                RuntimeException failure = new IllegalStateException("private-error-message");
                when(index.query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean()))
                        .thenThrow(failure);
                assertThat(catchThrowable(() -> traced.query(List.of(0.123456f), 3))).isSameAs(failure);
                assertThat(Span.current()).isSameAs(parent);
            } finally {
                parent.end();
            }
            provider.forceFlush().join(10, TimeUnit.SECONDS);
            List<io.opentelemetry.proto.trace.v1.Span> spans = new ArrayList<>();
            for (int i = 0; i < 6; i++) {
                ExportTraceServiceRequest request = requests.poll(5, TimeUnit.SECONDS);
                assertThat(request).isNotNull();
                assertThat(request.toString()).doesNotContain("private-filter", "private-email", "private-vector-id",
                        "private-error-message", "0.123456", "exception.stacktrace", "pinecone.filter");
                request.getResourceSpansList().forEach(resource -> resource.getScopeSpansList()
                        .forEach(scope -> spans.addAll(scope.getSpansList())));
            }
            assertThat(requests).isEmpty();
            assertThat(spans).hasSize(6);
            var parentProto = spans.stream().filter(span -> span.getName().equals("parent")).findFirst().orElseThrow();
            var operations = spans.stream().filter(span -> !span.getName().equals("parent")).toList();
            assertThat(operations).extracting(io.opentelemetry.proto.trace.v1.Span::getName)
                    .containsExactlyInAnyOrder("Pinecone Query", "Pinecone Query", "Pinecone Fetch", "Pinecone Upsert", "Pinecone Delete");
            assertThat(operations).allSatisfy(span -> {
                assertThat(span.getParentSpanId()).isEqualTo(parentProto.getSpanId());
                assertThat(span.getTraceId()).isEqualTo(parentProto.getTraceId());
                assertThat(span.getEndTimeUnixNano()).isGreaterThanOrEqualTo(span.getStartTimeUnixNano());
                assertThat(span.getAttributesList()).anySatisfy(attr -> {
                    assertThat(attr.getKey()).isEqualTo("db.system");
                    assertThat(attr.getValue().getStringValue()).isEqualTo("pinecone");
                });
            });
            assertThat(operations.stream().filter(span -> span.getStatus().getCode() == Status.StatusCode.STATUS_CODE_OK))
                    .hasSize(4);
            var failed = operations.stream().filter(span -> span.getStatus().getCode() == Status.StatusCode.STATUS_CODE_ERROR)
                    .findFirst().orElseThrow();
            assertThat(failed.getStatus().getMessage()).isEmpty();
            assertThat(failed.getEventsList()).singleElement().satisfies(event -> {
                assertThat(event.getName()).isEqualTo("exception");
                assertThat(event.getAttributesList()).singleElement().satisfies(attr -> {
                    assertThat(attr.getKey()).isEqualTo("exception.type");
                    assertThat(attr.getValue().getStringValue()).isEqualTo(IllegalStateException.class.getName());
                });
            });
            verify(index, times(2)).query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean());
            verify(index).fetch(any(), any());
            verify(index).upsert(anyList(), any());
            verify(index).deleteByIds(any(), any());
            verifyNoMoreInteractions(index);
        } finally {
            server.stop(0);
        }
    }
}
