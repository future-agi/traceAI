package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import com.microsoft.semantickernel.orchestration.ToolCallBehavior;
import com.microsoft.semantickernel.plugin.KernelPluginFactory;
import com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.Span;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Mono;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/** Decodes real OTLP/HTTP requests sent only to a loopback collector. */
class OtlpWireTest {
    private static final String PROMPT = "synthetic-private-prompt";

    @Test
    void exportsOneAgentAndOneModelSpanWithoutContent() throws Exception {
        List<Span> spans = exportHidden(tracer -> {
            try (LocalOpenAi openAi = new LocalOpenAi()) {
                Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, openAi.chatService()).build();
                new TracedKernel(kernel, tracer).invokePromptAsync(PROMPT).block();
            }
        });
        assertThat(spans).hasSize(2);
        Map<String, Span> byKind = spans.stream()
            .collect(Collectors.toMap(span -> attribute(span, "fi.span.kind"), span -> span));
        assertThat(byKind).containsOnlyKeys("AGENT", "LLM");
        Span agent = byKind.get("AGENT");
        Span llm = byKind.get("LLM");
        assertThat(llm.getParentSpanId()).isEqualTo(agent.getSpanId());
        assertThat(attribute(llm, "gen_ai.usage.input_tokens")).isEqualTo("7");
        assertThat(attribute(llm, "gen_ai.usage.total_tokens")).isEqualTo("10");
        assertThat(attribute(agent, "gen_ai.usage.input_tokens")).isNull();
    }

    @Test
    void nativeErrorDescriptionsAndExceptionsAreHiddenOnTheWire() throws Exception {
        List<Span> spans = exportHidden(tracer -> {
            try (LocalOpenAi openAi = new LocalOpenAi().mode(LocalOpenAi.Mode.SERVER_ERROR)) {
                Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, openAi.chatService()).build();
                assertThatThrownBy(() -> new TracedKernel(kernel, tracer).invokePromptAsync(PROMPT).block())
                    .hasMessageContaining(LocalOpenAi.ERROR_DETAIL);
            }
        });
        assertThat(spans).hasSize(2);
        assertThat(spans).allSatisfy(span -> {
            assertThat(span.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
            assertThat(span.getStatus().getMessage()).isEmpty();
        });
    }

    @Test
    void automaticToolErrorsAreHiddenOnTheWire() throws Exception {
        List<Span> spans = exportHidden(tracer -> {
            try (LocalOpenAi openAi = new LocalOpenAi().mode(LocalOpenAi.Mode.TOOL).tool("local-failing")) {
                Kernel kernel = Kernel.builder()
                    .withPlugin(KernelPluginFactory.createFromObject(new FailingPlugin(), "local")).build();
                InvocationContext context = InvocationContext.builder()
                    .withToolCallBehavior(ToolCallBehavior.allowAllKernelFunctions(true)).build();
                new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer)
                    .getChatMessageContentsAsync(PROMPT, kernel, context).block();
            }
        });
        List<Span> tools = spans.stream()
            .filter(span -> span.getName().startsWith("function_invocation")).collect(Collectors.toList());
        assertThat(tools).hasSize(1);
        Span tool = tools.get(0);
        assertThat(attribute(tool, "fi.span.kind")).isEqualTo("TOOL");
        assertThat(tool.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_ERROR);
        assertThat(tool.getStatus().getMessage()).isEmpty();
        assertThat(tool.getEventsList()).allSatisfy(event -> assertThat(event.getAttributesList())
            .extracting(KeyValue::getKey).doesNotContain("exception.message", "exception.stacktrace"));
    }

    public static class FailingPlugin {
        @DefineKernelFunction(name = "failing", returnType = "java.lang.String")
        public String failing() {
            throw new IllegalStateException(LocalOpenAi.ERROR_DETAIL);
        }
    }

    @Test
    void embeddingTextAndStreamingContentAreHiddenOnTheWire() throws Exception {
        List<Span> spans = exportHidden(tracer -> {
            TextEmbeddingGenerationService embedding = mock(TextEmbeddingGenerationService.class);
            when(embedding.generateEmbeddingsAsync(any())).thenReturn(Mono.just(List.of()));
            new TracedTextEmbeddingGenerationService(embedding, "embedding-model", "test", tracer)
                .generateEmbeddingsAsync(List.of(PROMPT)).block();
            try (LocalOpenAi openAi = new LocalOpenAi().mode(LocalOpenAi.Mode.STREAM)) {
                new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer)
                    .getStreamingChatMessageContentsAsync(PROMPT, null, null).blockLast();
            }
        });
        assertThat(spans).hasSize(2);
        assertThat(spans.stream().map(span -> attribute(span, "fi.span.kind")))
            .containsExactlyInAnyOrder("EMBEDDING", "LLM");
        assertThat(spans).allSatisfy(span -> assertThat(attribute(span, "gen_ai.usage.input_tokens")).isNull());
    }

    private static List<Span> exportHidden(Consumer<FITracer> operation) throws Exception {
        List<byte[]> bodies = new CopyOnWriteArrayList<>();
        HttpServer collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/v1/traces", exchange -> {
            bodies.add(exchange.getRequestBody().readAllBytes());
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        collector.start();
        GlobalOpenTelemetry.resetForTest();
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(OtlpHttpSpanExporter.builder()
                .setEndpoint("http://127.0.0.1:" + collector.getAddress().getPort() + "/v1/traces")
                .build())).build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        try (Scope ignored = Context.root().makeCurrent()) {
            operation.accept(new FITracer(provider.get("traceai-test"), TraceConfig.builder()
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build()));
            assertThat(provider.forceFlush().join(10, TimeUnit.SECONDS).isSuccess()).isTrue();
        } finally {
            provider.close();
            collector.stop(0);
            GlobalOpenTelemetry.resetForTest();
        }
        assertThat(bodies).isNotEmpty();
        for (byte[] body : bodies) {
            assertThat(new String(body, StandardCharsets.ISO_8859_1))
                .doesNotContain(PROMPT, LocalOpenAi.ANSWER, LocalOpenAi.ERROR_DETAIL);
        }
        return bodies.stream().map(OtlpWireTest::decode)
            .flatMap(request -> request.getResourceSpansList().stream())
            .flatMap(resource -> resource.getScopeSpansList().stream())
            .flatMap(scope -> scope.getSpansList().stream()).collect(Collectors.toList());
    }

    private static ExportTraceServiceRequest decode(byte[] body) {
        try {
            return ExportTraceServiceRequest.parseFrom(body);
        } catch (Exception e) {
            throw new AssertionError("Invalid OTLP request", e);
        }
    }

    private static String attribute(Span span, String key) {
        for (KeyValue attribute : span.getAttributesList()) {
            if (attribute.getKey().equals(key)) {
                var value = attribute.getValue();
                return value.hasIntValue() ? Long.toString(value.getIntValue()) : value.getStringValue();
            }
        }
        return null;
    }
}
