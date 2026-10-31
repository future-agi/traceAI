package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import com.sun.net.httpserver.HttpServer;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.listener.*;
import dev.langchain4j.model.output.TokenUsage;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.List;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.*;

class ListenerOtlpTest {
    @Test void exportsRealOtlpProtobufWithModelUsageParentAndPrivacyDefaults() throws Exception {
        LinkedBlockingQueue<byte[]> received = new LinkedBlockingQueue<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/traces", exchange -> {
            received.add(exchange.getRequestBody().readAllBytes());
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
        try (SdkTracerProvider sdk = SdkTracerProvider.builder().addSpanProcessor(BatchSpanProcessor.builder(
                OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + server.getAddress().getPort()
                        + "/v1/traces").build()).build()).build()) {
            LangChain4jListener listener = new LangChain4jListener(new FITracer(sdk.get("wire-test")), "synthetic");
            ChatModelRequest request = ChatModelRequest.builder().model("wire-model")
                    .messages(List.of(UserMessage.from("SECRET_PROMPT"))).build();
            Map<Object, Object> success = new ConcurrentHashMap<>();
            Map<Object, Object> failure = new ConcurrentHashMap<>();
            Span parent = sdk.get("wire-test").spanBuilder("parent").startSpan();
            try (Scope ignored = parent.makeCurrent()) {
                listener.onRequest(new ChatModelRequestContext(request, success));
                listener.onRequest(new ChatModelRequestContext(request, failure));
            }
            listener.onResponse(new ChatModelResponseContext(ChatModelResponse.builder().model("resolved-model")
                    .aiMessage(AiMessage.from(ToolExecutionRequest.builder().name("SECRET_TOOL")
                            .arguments("SECRET_ARGUMENTS").build())).tokenUsage(new TokenUsage(2, 3)).build(), request, success));
            listener.onError(new ChatModelErrorContext(new IllegalArgumentException("SECRET_ERROR"), request, null, failure));
            sdk.forceFlush().join(10, TimeUnit.SECONDS);
            byte[] bytes = received.poll(10, TimeUnit.SECONDS);
            assertThat(bytes).isNotNull();
            assertThat(new String(bytes, StandardCharsets.UTF_8)).doesNotContain("SECRET_");
            ExportTraceServiceRequest wire = ExportTraceServiceRequest.parseFrom(bytes);
            List<io.opentelemetry.proto.trace.v1.Span> spans = wire.getResourceSpansList().stream()
                    .flatMap(resource -> resource.getScopeSpansList().stream())
                    .flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).hasSize(2).allSatisfy(span -> {
                assertThat(java.util.HexFormat.of().formatHex(span.getParentSpanId().toByteArray()))
                        .isEqualTo(parent.getSpanContext().getSpanId());
                assertThat(span.getEndTimeUnixNano()).isGreaterThanOrEqualTo(span.getStartTimeUnixNano());
            });
            assertThat(spans.get(0).getAttributesList()).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo("gen_ai.usage.total_tokens");
                assertThat(attribute.getValue().getIntValue()).isEqualTo(5);
            });
            assertThat(spans.get(1).getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
            assertThat(spans.get(1).getStatus().getMessage()).isEmpty();
            parent.end();
        } finally {
            server.stop(0);
        }
    }
}
