package ai.traceai.compat;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import ai.traceai.langchain4j.LangChain4jListener;
import com.sun.net.httpserver.HttpServer;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.chat.listener.ChatModelListener;
import dev.langchain4j.model.chat.request.ChatRequest;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.output.TokenUsage;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.*;

class ModernWireTest {
    @Test void modernNativeCallbacksExportOtlpAndContentOptInCannotOverrideHideFlags() throws Exception {
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
            for (boolean optIn : new boolean[]{false, true}) {
                FITracer tracer = new FITracer(sdk.get("wire"), TraceConfig.builder()
                        .hideInputs(optIn).hideOutputs(optIn).hideErrorDetails(optIn).build());
                LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic", optIn);
                ChatModel model = new ChatModel() {
                    @Override public List<ChatModelListener> listeners() { return List.of(listener); }
                    @Override public ChatResponse doChat(ChatRequest request) {
                        return ChatResponse.builder().modelName("resolved-model").aiMessage(AiMessage.from(
                                ToolExecutionRequest.builder().name("SECRET_TOOL").arguments("SECRET_ARGUMENTS").build()))
                                .tokenUsage(new TokenUsage(2, 3)).build();
                    }
                };
                model.chat("SECRET_PROMPT");
            }
            sdk.forceFlush().join(10, TimeUnit.SECONDS);
            byte[] bytes = received.poll(10, TimeUnit.SECONDS);
            assertThat(bytes).isNotNull();
            assertThat(new String(bytes, StandardCharsets.UTF_8)).doesNotContain("SECRET_");
            var spans = ExportTraceServiceRequest.parseFrom(bytes).getResourceSpansList().stream()
                    .flatMap(resource -> resource.getScopeSpansList().stream())
                    .flatMap(scope -> scope.getSpansList().stream()).toList();
            assertThat(spans).hasSize(2).allSatisfy(span -> {
                assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                    assertThat(attribute.getKey()).isEqualTo("gen_ai.response.model");
                    assertThat(attribute.getValue().getStringValue()).isEqualTo("resolved-model");
                });
                assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                    assertThat(attribute.getKey()).isEqualTo("gen_ai.usage.total_tokens");
                    assertThat(attribute.getValue().getIntValue()).isEqualTo(5);
                });
            });
        } finally {
            server.stop(0);
        }
    }
}
