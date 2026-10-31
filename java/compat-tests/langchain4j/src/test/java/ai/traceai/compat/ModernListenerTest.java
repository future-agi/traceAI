package ai.traceai.compat;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import ai.traceai.langchain4j.LangChain4jListener;
import ai.traceai.langchain4j.TracedAiServices;
import dev.langchain4j.agent.tool.Tool;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.chat.StreamingChatModel;
import dev.langchain4j.model.chat.listener.ChatModelListener;
import dev.langchain4j.model.chat.request.ChatRequest;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.chat.response.StreamingChatResponseHandler;
import dev.langchain4j.model.output.TokenUsage;
import dev.langchain4j.service.AiServices;
import dev.langchain4j.service.TokenStream;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.*;

class ModernListenerTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider sdk = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final FITracer tracer = new FITracer(sdk.get("test"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
    private final LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic");
    private final ChatRequest request = ChatRequest.builder().modelName("test-model")
            .messages(UserMessage.from("PRIVATE_PROMPT")).build();
    private final ChatResponse response = ChatResponse.builder().modelName("resolved-model")
            .aiMessage(AiMessage.from("PRIVATE_RESPONSE")).tokenUsage(new TokenUsage(2, 3)).build();
    @AfterEach void close() { sdk.close(); }

    @Test void packagedListenerRunsWithoutTheRemovedChatLanguageModelClass() {
        assertThatThrownBy(() -> Class.forName("dev.langchain4j.model.chat.ChatLanguageModel"))
                .isInstanceOf(ClassNotFoundException.class);
        ChatModel model = new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public ChatResponse doChat(ChatRequest actual) { return response; }
        };
        assertThat(model.chat(request)).isSameAs(response);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getAttributes().get(AttributeKey.stringKey("gen_ai.request.model"))).isEqualTo("test-model");
            assertThat(span.getAttributes().get(AttributeKey.stringKey("gen_ai.response.model"))).isEqualTo("resolved-model");
            assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(5);
            assertThat(span.getAttributes().toString()).doesNotContain("PRIVATE_");
        });
    }

    @Test void optInReadsModernMessageAndToolPayloadTypesFromTheLegacyCompiledJar() {
        LangChain4jListener capturing = new LangChain4jListener(new FITracer(sdk.get("test")), "synthetic", true);
        ChatModel model = new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(capturing); }
            @Override public ChatResponse doChat(ChatRequest actual) {
                return ChatResponse.builder().aiMessage(AiMessage.from(ToolExecutionRequest.builder()
                        .name("lookup").arguments("PRIVATE_ARGUMENTS").build())).build();
            }
        };
        model.chat(request);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getAttributes().toString()).contains("PRIVATE_PROMPT", "PRIVATE_ARGUMENTS");
            assertThat(span.getAttributes().get(AttributeKey.stringKey("traceai.instrumentation.error"))).isNull();
        });
    }

    @Test void streamingWaitsForTerminalCallbackAndKeepsTheOriginalParentAcrossThreads() {
        AtomicReference<StreamingChatResponseHandler> pending = new AtomicReference<>();
        StreamingChatModel model = new StreamingChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public void doChat(ChatRequest request, StreamingChatResponseHandler handler) { pending.set(handler); }
        };
        CompletableFuture<ChatResponse> result = new CompletableFuture<>();
        StringBuilder chunks = new StringBuilder();
        Span parent = sdk.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            model.chat(request, new StreamingChatResponseHandler() {
                @Override public void onPartialResponse(String part) { chunks.append(part); }
                @Override public void onCompleteResponse(ChatResponse response) { result.complete(response); }
                @Override public void onError(Throwable error) { result.completeExceptionally(error); }
            });
            assertThat(Span.current()).isSameAs(parent);
        }
        pending.get().onPartialResponse("PRIVATE_CHUNK");
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        CompletableFuture.runAsync(() -> pending.get().onCompleteResponse(response)).join();
        assertThat(result.join()).isSameAs(response);
        assertThat(chunks).hasToString("PRIVATE_CHUNK");
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getAttributes().toString()).doesNotContain("PRIVATE_");
        });
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
        parent.end();
    }

    @Test void asyncAndStreamingErrorsEndTheSameSpanWithoutLeakingDetails() {
        CompletableFuture<ChatResponse> pending = new CompletableFuture<>();
        ChatModel async = new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public CompletableFuture<ChatResponse> doChatAsync(ChatRequest request) { return pending; }
        };
        CompletableFuture<ChatResponse> result = async.chatAsync(request);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        IllegalStateException failure = new IllegalStateException("PRIVATE_ERROR");
        CompletableFuture.runAsync(() -> pending.completeExceptionally(failure)).join();
        assertThatThrownBy(result::join).hasCause(failure);
        StreamingChatModel streaming = new StreamingChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public void doChat(ChatRequest request, StreamingChatResponseHandler handler) { handler.onError(failure); }
        };
        AtomicReference<Throwable> observed = new AtomicReference<>();
        streaming.chat(request, new StreamingChatResponseHandler() {
            @Override public void onPartialResponse(String part) {}
            @Override public void onCompleteResponse(ChatResponse response) { throw new AssertionError(); }
            @Override public void onError(Throwable error) { observed.set(error); }
        });
        assertThat(observed.get()).isSameAs(failure);
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEmpty();
            assertThat(span.getAttributes().toString() + span.getEvents()).doesNotContain("PRIVATE_");
        });
    }

    @Test void asyncSuccessEndsOnCompletionNotOnFutureCreation() {
        CompletableFuture<ChatResponse> pending = new CompletableFuture<>();
        ChatModel model = new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public CompletableFuture<ChatResponse> doChatAsync(ChatRequest request) { return pending; }
        };
        CompletableFuture<ChatResponse> result = model.chatAsync(request);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        CompletableFuture.runAsync(() -> pending.complete(response)).join();
        assertThat(result.join()).isSameAs(response);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    interface Assistant { String chat(String input); }
    static class Tools {
        @Tool("Return a synthetic value") public String lookup() { return "PRIVATE_TOOL_RESULT"; }
    }

    @Test void actualAiServiceToolLoopWorksWithTheOfficialListenerAndLegacyServiceEntryPoint() {
        AtomicInteger calls = new AtomicInteger();
        ChatModel model = new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public ChatResponse doChat(ChatRequest actual) {
                if (calls.getAndIncrement() == 0) return ChatResponse.builder().aiMessage(AiMessage.from(
                        ToolExecutionRequest.builder().id("call-1").name("lookup").arguments("{}").build())).build();
                return response;
            }
        };
        Assistant original = AiServices.builder(Assistant.class).chatModel(model).tools(new Tools()).build();
        Assistant traced = TracedAiServices.create(Assistant.class, original, tracer);
        assertThat(traced.chat("PRIVATE_PROMPT")).isEqualTo("PRIVATE_RESPONSE");
        assertThat(calls).hasValue(2);
        assertThat(exporter.getFinishedSpanItems()).hasSize(3);
        var agent = exporter.getFinishedSpanItems().get(2);
        assertThat(agent.getName()).startsWith("AiService.");
        assertThat(exporter.getFinishedSpanItems().subList(0, 2)).allSatisfy(span ->
                assertThat(span.getParentSpanId()).isEqualTo(agent.getSpanId()));
        assertThat(exporter.getFinishedSpanItems()).allSatisfy(span ->
                assertThat(span.getAttributes().toString()).doesNotContain("PRIVATE_"));
    }

    interface StreamingAssistant { TokenStream chat(String input); }
    @Test void tokenStreamIsDelegatedWithoutReportingItsConstructionAsSuccess() {
        StreamingChatModel model = new StreamingChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public void doChat(ChatRequest request, StreamingChatResponseHandler handler) {
                handler.onCompleteResponse(response);
            }
        };
        StreamingAssistant original = AiServices.builder(StreamingAssistant.class).streamingChatModel(model).build();
        StreamingAssistant traced = TracedAiServices.create(StreamingAssistant.class, original, tracer);
        TokenStream stream = traced.chat("PRIVATE_PROMPT");
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        CompletableFuture<ChatResponse> result = new CompletableFuture<>();
        stream.onPartialResponse(part -> {}).onCompleteResponse(result::complete).onError(result::completeExceptionally).start();
        // AiServices combines streaming usage into a new response object.
        assertThat(result.join()).isEqualTo(response);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span ->
                assertThat(span.getName()).isEqualTo("LangChain4j Chat"));
    }
}
