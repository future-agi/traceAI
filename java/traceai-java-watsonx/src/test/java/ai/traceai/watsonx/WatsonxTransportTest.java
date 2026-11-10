package ai.traceai.watsonx;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import ai.traceai.SemanticConventions;
import com.ibm.watsonx.ai.textgeneration.TextGenerationHandler;
import com.ibm.watsonx.ai.textgeneration.TextGenerationParameters;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Status;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicReference;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;
import com.ibm.watsonx.ai.chat.ChatRequest;
import com.ibm.watsonx.ai.chat.ChatResponse;
import com.ibm.watsonx.ai.chat.ChatService;
import com.ibm.watsonx.ai.chat.model.AssistantMessage;
import com.ibm.watsonx.ai.chat.model.ChatParameters;
import com.ibm.watsonx.ai.chat.model.UserMessage;
import com.ibm.watsonx.ai.embedding.EmbeddingResponse;
import com.ibm.watsonx.ai.embedding.EmbeddingService;
import com.ibm.watsonx.ai.textgeneration.TextGenerationRequest;
import com.ibm.watsonx.ai.textgeneration.TextGenerationResponse;
import com.ibm.watsonx.ai.textgeneration.TextGenerationService;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/** Real released IBM services and a loopback-only fake HTTP transport, never IBM APIs. */
class WatsonxTransportTest {
    static final String TEXT = """
        {"model_id":"test-model","created_at":"2026-01-01T00:00:00Z","results":[
        {"generated_text":"offline answer","stop_reason":"eos_token","input_token_count":3,"generated_token_count":2}]}
        """;
    static final String EMBED = """
        {"model_id":"test-embedding","created_at":"2026-01-01T00:00:00Z","input_token_count":3,
        "results":[{"embedding":[0.1,0.2],"input":"offline prompt"}]}
        """;
    static final String CHAT = """
        {"id":"chat-test","model_id":"test-model","created":1,"choices":[
        {"index":0,"message":{"role":"assistant","content":"offline answer"},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}
        """;
    HttpServer server;
    InMemorySpanExporter exporter;
    SdkTracerProvider provider;
    FITracer tracer;
    final AtomicInteger requests = new AtomicInteger();

    @BeforeEach
    void setup() throws Exception {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("watsonx-test"), TraceConfig.builder().build());
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            requests.incrementAndGet();
            exchange.getRequestBody().readAllBytes();
            String path = exchange.getRequestURI().getPath();
            boolean stream = path.endsWith("_stream");
            String json = path.contains("embeddings") ? EMBED : path.contains("chat") ? CHAT : TEXT;
            if (stream && path.contains("chat")) json = json.replace("\"message\"", "\"delta\"");
            byte[] body = (stream ? "data: " + json.replace("\n", "") + "\n\n" : json).getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", stream ? "text/event-stream" : "application/json");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
    }

    @AfterEach
    void cleanup() {
        server.stop(0);
        provider.close();
    }

    // Only authentication setup changed between the tested IBM releases. No real token is read.
    <T> T service(Object builder, Class<T> type) throws Exception {
        builder.getClass().getMethod("baseUrl", String.class).invoke(builder, "http://127.0.0.1:" + server.getAddress().getPort());
        builder.getClass().getMethod("projectId", String.class).invoke(builder, "offline-project");
        builder.getClass().getMethod("modelId", String.class).invoke(builder, "test-model");
        Class<?> auth;
        String setter;
        try {
            auth = Class.forName("com.ibm.watsonx.ai.core.auth.Authenticator");
            setter = "authenticator";
        } catch (ClassNotFoundException oldSdk) {
            auth = Class.forName("com.ibm.watsonx.ai.core.auth.AuthenticationProvider");
            setter = "authenticationProvider";
        }
        Object stub = Proxy.newProxyInstance(auth.getClassLoader(), new Class<?>[]{auth}, (proxy, method, args) -> {
            if (method.getReturnType() == CompletableFuture.class) return CompletableFuture.completedFuture("offline-token");
            return "offline-token";
        });
        builder.getClass().getMethod(setter, auth).invoke(builder, stub);
        return type.cast(builder.getClass().getMethod("build").invoke(builder));
    }

    @Test
    void existingGenerateEntryPointCallsReleasedSdk() throws Exception {
        var service = service(TextGenerationService.builder(), TextGenerationService.class);
        Object request = TextGenerationRequest.builder().input("offline prompt").build();
        TextGenerationResponse response = new TracedWatsonxAI(service, tracer).generateText(request);
        assertThat(response.toText()).isEqualTo("offline answer");
        assertThat(requests).hasValue(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void existingEmbeddingEntryPointCallsReleasedSdk() throws Exception {
        var service = service(EmbeddingService.builder(), EmbeddingService.class);
        EmbeddingResponse response = new TracedWatsonxAI(service, tracer).embedText((Object) List.of("offline prompt"));
        assertThat(response.results().get(0).embedding()).hasSize(2);
        var attributes = exporter.getFinishedSpanItems().get(0).getAttributes();
        assertThat(attributes.get(io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS))).isEqualTo(2);
        assertThat(attributes.get(io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT))).isEqualTo(1);
        assertThat(requests).hasValue(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void existingChatEntryPointCallsReleasedSdk() throws Exception {
        var service = service(ChatService.builder(), ChatService.class);
        Object request = ChatRequest.builder().messages(List.of(UserMessage.text("offline prompt"))).build();
        ChatResponse response = new TracedWatsonxAI(service, tracer).chat(request);
        assertThat(response).isNotNull();
        var attributes = exporter.getFinishedSpanItems().get(0).getAttributes();
        assertThat(attributes.get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES)))
            .contains("offline prompt");
        assertThat(attributes.get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo("offline answer");
        assertThat(attributes.get(io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(5);
        assertThat(requests).hasValue(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void existingStreamingEntryPointCallsReleasedSdk() throws Exception {
        var service = service(TextGenerationService.builder(), TextGenerationService.class);
        Object request = TextGenerationRequest.builder().input("offline prompt").build();
        CompletableFuture<Void> result = new TracedWatsonxAI(service, tracer).generateTextStream(request);
        result.get(10, TimeUnit.SECONDS);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(requests).hasValue(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void realChatStreamingDeliversResponseAndUsage() throws Exception {
        var service = service(ChatService.builder(), ChatService.class);
        var handler = mock(com.ibm.watsonx.ai.chat.ChatHandler.class);
        CompletableFuture<?> future = new TracedWatsonxAI(service, tracer).chatStream(
            ChatRequest.builder().messages(List.of(UserMessage.text("offline prompt"))).build(), handler);
        future.get(10, TimeUnit.SECONDS);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        verify(handler).onCompleteResponse(any(ChatResponse.class));
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
            .get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo("offline answer");
    }

    @Test
    void realStreamingTransportKeepsSpanOpenBetweenChunks() throws Exception {
        CountDownLatch partial = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        AtomicReference<Throwable> failure = new AtomicReference<>();
        server.removeContext("/");
        server.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try {
                String first = TEXT.replace("eos_token", "not_finished").replace("offline answer", "offline ");
                exchange.getResponseBody().write(("data: " + first.replace("\n", "") + "\n\n").getBytes(StandardCharsets.UTF_8));
                exchange.getResponseBody().flush();
                if (!release.await(10, TimeUnit.SECONDS)) throw new IllegalStateException("stream test timed out");
                String last = TEXT.replace("offline answer", "answer");
                exchange.getResponseBody().write(("data: " + last.replace("\n", "") + "\n\n").getBytes(StandardCharsets.UTF_8));
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            } finally { exchange.close(); }
        });
        var service = service(TextGenerationService.builder(), TextGenerationService.class);
        try {
            CompletableFuture<Void> result = new TracedWatsonxAI(service, tracer).generateTextStream(
                TextGenerationRequest.builder().input("offline prompt").build(), new TextGenerationHandler() {
                    @Override public void onPartialResponse(String text) { partial.countDown(); }
                    @Override public void onCompleteResponse(TextGenerationResponse response) { }
                    @Override public void onError(Throwable error) { failure.set(error); }
                });
            assertThat(partial.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(result.isDone()).isFalse();
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            release.countDown();
            result.get(10, TimeUnit.SECONDS);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(failure).hasValue(null);
            assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
                .get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
                .isEqualTo("offline answer");
        } finally { release.countDown(); }
    }

    @Test
    void otlpWireCarriesModelUsageAndContent() throws Exception {
        List<ExportTraceServiceRequest> wire = new CopyOnWriteArrayList<>();
        try (SdkTracerProvider otlp = wireProvider(wire)) {
            FITracer tracer = new FITracer(otlp.get("wire"), TraceConfig.builder().build());
            var generation = service(TextGenerationService.builder(), TextGenerationService.class);
            new TracedWatsonxAI(generation, tracer).generateText(TextGenerationRequest.builder()
                .input("offline prompt").parameters(TextGenerationParameters.builder().modelId("requested-model").build()).build());
            otlp.forceFlush().join(5, TimeUnit.SECONDS);
            assertThat(wire).hasSize(1);
            var span = wire.get(0).getResourceSpans(0).getScopeSpans(0).getSpans(0);
            assertThat(span.getName()).isEqualTo("Watsonx Text Generation");
            assertThat(span.getStatus().getCode()).isEqualTo(Status.StatusCode.STATUS_CODE_OK);
            assertThat(span.getAttributesList()).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo(SemanticConventions.LLM_REQUEST_MODEL);
                assertThat(attribute.getValue().getStringValue()).isEqualTo("requested-model");
            }).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo(SemanticConventions.LLM_TOKEN_COUNT_TOTAL);
                assertThat(attribute.getValue().getIntValue()).isEqualTo(5);
            }).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo(SemanticConventions.OUTPUT_VALUE);
                assertThat(attribute.getValue().getStringValue()).isEqualTo("offline answer");
            });
        }
    }

    @Test
    void otlpWireCarriesLastChatUserInputAndCompletionLimit() throws Exception {
        List<ExportTraceServiceRequest> wire = new CopyOnWriteArrayList<>();
        try (SdkTracerProvider otlp = wireProvider(wire)) {
            FITracer wireTracer = new FITracer(otlp.get("wire"), TraceConfig.builder().build());
            ChatRequest request = ChatRequest.builder()
                .messages(List.of(UserMessage.text("earlier prompt"), UserMessage.text("offline prompt"),
                    AssistantMessage.text("earlier answer")))
                .parameters(ChatParameters.builder().maxCompletionTokens(256).build())
                .build();
            new TracedWatsonxAI(service(ChatService.builder(), ChatService.class), wireTracer).chat(request);
            otlp.forceFlush().join(5, TimeUnit.SECONDS);
            assertThat(wire).hasSize(1);
            var attributes = wire.get(0).getResourceSpans(0).getScopeSpans(0).getSpans(0).getAttributesList();
            assertThat(attributes).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo(SemanticConventions.INPUT_VALUE);
                assertThat(attribute.getValue().getStringValue()).isEqualTo("offline prompt");
            }).anySatisfy(attribute -> {
                assertThat(attribute.getKey()).isEqualTo(SemanticConventions.LLM_REQUEST_MAX_TOKENS);
                assertThat(attribute.getValue().getIntValue()).isEqualTo(256);
            });
        }
    }

    @Test
    void otlpWireHidesContentIncludingEmbeddingEchoAndErrors() throws Exception {
        List<ExportTraceServiceRequest> wire = new CopyOnWriteArrayList<>();
        try (SdkTracerProvider otlp = wireProvider(wire)) {
            FITracer privateTracer = new FITracer(otlp.get("wire"), TraceConfig.builder()
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
            var generation = service(TextGenerationService.builder(), TextGenerationService.class);
            var embedding = service(EmbeddingService.builder(), EmbeddingService.class);
            var chat = service(ChatService.builder(), ChatService.class);
            var request = TextGenerationRequest.builder().input("offline prompt").build();
            new TracedWatsonxAI(generation, privateTracer).generateText(request);
            new TracedWatsonxAI(embedding, privateTracer).embedText(List.of("offline prompt"));
            new TracedWatsonxAI(chat, privateTracer).chat(ChatRequest.builder().messages(List.of(UserMessage.text("offline prompt"))).build());
            TextGenerationService broken = mock(TextGenerationService.class);
            IllegalArgumentException error = new IllegalArgumentException("private-error-detail");
            when(broken.generate(request)).thenThrow(error);
            assertThatThrownBy(() -> new TracedWatsonxAI(broken, privateTracer).generateText(request)).isSameAs(error);
            otlp.forceFlush().join(5, TimeUnit.SECONDS);
            assertThat(wire).hasSize(4);
            assertThat(wire.toString()).doesNotContain("offline prompt", "offline answer", "private-error-detail");
            assertThat(wire.toString()).contains("test-model", SemanticConventions.LLM_TOKEN_COUNT_PROMPT, "exception.type");
        }
    }

    @Test
    void hideInputsAloneDoesNotLeakEmbeddingEcho() throws Exception {
        FITracer privateTracer = new FITracer(provider.get("test"), TraceConfig.builder().hideInputs(true).build());
        new TracedWatsonxAI(service(EmbeddingService.builder(), EmbeddingService.class), privateTracer)
            .embedText(List.of("offline prompt"));
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes().toString()).doesNotContain("offline prompt");
    }

    @Test
    void messageHidingAlsoHidesChatRawPayloads() throws Exception {
        List<ExportTraceServiceRequest> wire = new CopyOnWriteArrayList<>();
        try (SdkTracerProvider otlp = wireProvider(wire)) {
            FITracer privateTracer = new FITracer(otlp.get("wire"), TraceConfig.builder()
                .hideInputMessages(true).hideOutputMessages(true).build());
            new TracedWatsonxAI(service(ChatService.builder(), ChatService.class), privateTracer)
                .chat(ChatRequest.builder().messages(List.of(UserMessage.text("offline prompt"))).build());
            otlp.forceFlush().join(5, TimeUnit.SECONDS);
            assertThat(wire).hasSize(1);
            var attributes = wire.get(0).getResourceSpans(0).getScopeSpans(0).getSpans(0).getAttributesList();
            assertThat(attributes.toString()).doesNotContain("offline prompt", "offline answer");
            assertThat(attributes).noneMatch(attribute ->
                attribute.getKey().equals(SemanticConventions.INPUT_VALUE)
                    || attribute.getKey().equals(SemanticConventions.RAW_INPUT));
        }
    }

    SdkTracerProvider wireProvider(List<ExportTraceServiceRequest> wire) {
        server.createContext("/v1/traces", exchange -> {
            wire.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody()));
            exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        var exporter = OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/traces").build();
        return SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    }
}
