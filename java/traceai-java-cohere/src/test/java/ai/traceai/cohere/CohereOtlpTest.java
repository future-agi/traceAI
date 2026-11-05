package ai.traceai.cohere;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.cohere.api.resources.v2.requests.V2ChatRequest;
import com.cohere.api.resources.v2.requests.V2EmbedRequest;
import com.cohere.api.resources.v2.requests.V2RerankRequest;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Span;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static ai.traceai.cohere.TracedCohereTransportTest.*;
import static org.assertj.core.api.Assertions.*;

class CohereOtlpTest {
    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void exportsOneOtlpSpanPerOperationWithPrivacyAndUsage(boolean hide) throws Exception {
        var bodies = new LinkedBlockingQueue<byte[]>();
        var contentTypes = new LinkedBlockingQueue<String>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            contentTypes.add(exchange.getRequestHeaders().getFirst("Content-Type"));
            bodies.add(exchange.getRequestBody().readAllBytes());
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(
                OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces")
                    .setTimeout(Duration.ofSeconds(5)).build())).build()) {
            var transport = new FakeTransport(CHAT_RESPONSE);
            var config = TraceConfig.builder().hideInputs(hide).hideOutputs(hide).hideErrorDetails(hide).build();
            var traced = new TracedCohereClient(transport.client(), new FITracer(provider.get("cohere-wire"), config));
            traced.v2().chat(json(CHAT_INPUT, V2ChatRequest.class));
            transport.body = V2_STREAM;
            try (var stream = stream(traced, true)) { stream.forEach(event -> {}); }
            transport.body = V1_STREAM;
            try (var stream = stream(traced, false)) { stream.forEach(event -> {}); }
            transport.body = "{\"id\":\"e\",\"embeddings\":{\"float\":[[0.1,0.2]]}}";
            traced.v2().embed(json("{\"model\":\"embed\",\"input_type\":\"search_query\",\"texts\":[\"private-input\"]}", V2EmbedRequest.class));
            transport.body = "{\"results\":[{\"index\":0,\"relevance_score\":0.9}]}";
            traced.v2().rerank(json("{\"model\":\"rerank\",\"query\":\"private-query\",\"documents\":[\"private-doc\"]}", V2RerankRequest.class));
            transport.body = "{\"message\":\"private-error\"}";
            transport.code = 401;
            assertThatThrownBy(() -> traced.v2().chat(json(CHAT_INPUT, V2ChatRequest.class)))
                .isInstanceOf(com.cohere.api.errors.UnauthorizedError.class);
            assertThat(provider.forceFlush().join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
            List<Span> spans = new ArrayList<>();
            for (int i = 0; i < 6; i++) {
                byte[] bytes = bodies.poll(5, TimeUnit.SECONDS);
                assertThat(bytes).isNotNull();
                assertThat(contentTypes.poll()).isEqualTo("application/x-protobuf");
                var request = ExportTraceServiceRequest.parseFrom(bytes);
                request.getResourceSpansList().forEach(resource -> resource.getScopeSpansList()
                    .forEach(scope -> spans.addAll(scope.getSpansList())));
                if (hide) assertThat(request.toString()).doesNotContain("private-");
            }
            assertThat(bodies).isEmpty();
            assertThat(spans).hasSize(6);
            assertThat(spans.stream().map(Span::getSpanId).distinct()).hasSize(6);
            var chats = spans.stream().filter(span -> span.getName().contains("Chat")
                && span.getStatus().getCode() == Status.StatusCode.STATUS_CODE_OK).toList();
            assertThat(chats).hasSize(3);
            for (Span span : chats) {
                assertThat(span.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_OK);
                assertThat(attribute(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL).getIntValue()).isEqualTo(8);
                assertThat(attribute(span, SemanticConventions.LLM_PROVIDER).getStringValue()).isEqualTo("cohere");
                assertThat(span.getEndTimeUnixNano()).isGreaterThanOrEqualTo(span.getStartTimeUnixNano());
                if (!hide) assertThat(attribute(span, SemanticConventions.OUTPUT_VALUE).getStringValue()).isEqualTo("private-output");
            }
            Span error = spans.stream().filter(span -> span.getStatus().getCode() == Status.StatusCode.STATUS_CODE_ERROR)
                .findFirst().orElseThrow();
            assertThat(error.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
            assertThat(error.getEventsCount()).isEqualTo(1);
            if (hide) {
                assertThat(error.getStatus().getMessage()).isEmpty();
                assertThat(error.getEvents(0).getAttributesList()).hasSize(1);
                assertThat(error.getEvents(0).getAttributes(0).getKey()).isEqualTo("exception.type");
            }
        } finally {
            server.stop(0);
        }
    }

    private io.opentelemetry.proto.common.v1.AnyValue attribute(Span span, String key) {
        return span.getAttributesList().stream().filter(attribute -> attribute.getKey().equals(key))
            .findFirst().orElseThrow().getValue();
    }
}
