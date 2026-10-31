package ai.traceai.qdrant;

import ai.traceai.FITracer;
import com.sun.net.httpserver.HttpServer;
import io.grpc.Server;
import io.grpc.ServerBuilder;
import io.grpc.stub.StreamObserver;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.qdrant.client.QdrantClient;
import io.qdrant.client.QdrantGrpcClient;
import io.qdrant.client.grpc.Points.*;
import io.qdrant.client.grpc.PointsGrpc;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static io.qdrant.client.ValueFactory.value;
import static org.assertj.core.api.Assertions.*;

/** Real SDK gRPC and decoded OTLP over loopback only; no backend or API keys. */
class TracedQdrantWireTest {
    @Test
    void realSdkExportsCompletedQueryAndPrivateFailureOverOtlp() throws Exception {
        LinkedBlockingQueue<byte[]> exports = new LinkedBlockingQueue<>();
        LinkedBlockingQueue<QueryPoints> requests = new LinkedBlockingQueue<>();
        LinkedBlockingQueue<StreamObserver<QueryResponse>> replies = new LinkedBlockingQueue<>();
        HttpServer collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/v1/traces", exchange -> {
            exports.add(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().add("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        collector.start();
        Server server = ServerBuilder.forPort(0).addService(new PointsGrpc.PointsImplBase() {
            @Override
            public void query(QueryPoints request, StreamObserver<QueryResponse> responseObserver) {
                requests.add(request);
                replies.add(responseObserver);
            }
        }).build().start();
        try (var exporter = OtlpHttpSpanExporter.builder()
                    .setEndpoint("http://127.0.0.1:" + collector.getAddress().getPort() + "/v1/traces").build();
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
             var client = new QdrantClient(QdrantGrpcClient
                     .newBuilder("127.0.0.1", server.getPort(), false, false).build())) {
            var traced = new TracedQdrantClient(client,
                    new FITracer(provider.get("qdrant-wire"), TracedQdrantClientTest.config()));
            QueryPoints request = QueryPoints.newBuilder().setCollectionName("documents").setLimit(3)
                    .addPrefetch(PrefetchQuery.newBuilder().setLimit(10)).build();
            Span parent = provider.get("test").spanBuilder("parent").startSpan();
            try {
                com.google.common.util.concurrent.ListenableFuture<java.util.List<ScoredPoint>> future;
                try (Scope ignored = parent.makeCurrent()) {
                    future = traced.queryAsync(request);
                    assertThat(Span.current()).isSameAs(parent);
                }
                assertThat(requests.poll(5, TimeUnit.SECONDS)).isEqualTo(request);
                StreamObserver<QueryResponse> reply = replies.poll(5, TimeUnit.SECONDS);
                assertThat(reply).isNotNull();
                assertThat(future.isDone()).isFalse();
                assertThat(exports).isEmpty();
                ScoredPoint point = ScoredPoint.newBuilder().setScore(0.9f)
                        .putPayload("text", value("private-response-sentinel")).build();
                reply.onNext(QueryResponse.newBuilder().addResult(point).build());
                reply.onCompleted();
                assertThat(future.get(5, TimeUnit.SECONDS)).containsExactly(point);
                byte[] payload = exports.poll(5, TimeUnit.SECONDS);
                assertThat(payload).isNotNull();
                var span = onlySpan(payload);
                assertThat(span.getName()).isEqualTo("Qdrant Query");
                assertThat(span.getTraceId().toByteArray()).isEqualTo(parent.getSpanContext().getTraceIdBytes());
                assertThat(span.getParentSpanId().toByteArray()).isEqualTo(parent.getSpanContext().getSpanIdBytes());
                assertThat(span.getEndTimeUnixNano()).isGreaterThan(span.getStartTimeUnixNano());
                assertThat(span.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_OK);
                assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                    assertThat(attribute.getKey()).isEqualTo("db.vector.results.count");
                    assertThat(attribute.getValue().getIntValue()).isEqualTo(1);
                });
                assertThat(new String(payload, StandardCharsets.ISO_8859_1)).doesNotContain("private-response-sentinel");

                var failed = traced.queryAsync(request);
                assertThat(requests.poll(5, TimeUnit.SECONDS)).isEqualTo(request);
                var failureReply = replies.poll(5, TimeUnit.SECONDS);
                assertThat(failureReply).isNotNull();
                failureReply.onError(io.grpc.Status.INTERNAL.withDescription("private-error-sentinel").asRuntimeException());
                assertThatThrownBy(() -> failed.get(5, TimeUnit.SECONDS)).hasMessageContaining("private-error-sentinel");
                byte[] failedPayload = exports.poll(5, TimeUnit.SECONDS);
                assertThat(failedPayload).isNotNull();
                var failedSpan = onlySpan(failedPayload);
                assertThat(failedSpan.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
                assertThat(failedSpan.getStatus().getMessage()).isEmpty();
                assertThat(failedSpan.getEventsCount()).isEqualTo(1);
                assertThat(new String(failedPayload, StandardCharsets.ISO_8859_1)).doesNotContain("private-error-sentinel");
                assertThat(exports).isEmpty();
            } finally {
                parent.end();
            }
        } finally {
            server.shutdownNow().awaitTermination(5, TimeUnit.SECONDS);
            collector.stop(0);
        }
    }

    private io.opentelemetry.proto.trace.v1.Span onlySpan(byte[] payload) throws Exception {
        var request = ExportTraceServiceRequest.parseFrom(payload);
        assertThat(request.getResourceSpansCount()).isEqualTo(1);
        var resource = request.getResourceSpans(0);
        assertThat(resource.getScopeSpansCount()).isEqualTo(1);
        assertThat(resource.getScopeSpans(0).getSpansCount()).isEqualTo(1);
        return resource.getScopeSpans(0).getSpans(0);
    }
}
