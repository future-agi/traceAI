package ai.traceai.compat;

import ai.traceai.FITracer;
import ai.traceai.langchain4j.LangChain4jListener;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.chat.StreamingChatModel;
import dev.langchain4j.model.chat.listener.ChatModelListener;
import dev.langchain4j.model.chat.request.ChatRequest;
import dev.langchain4j.model.chat.response.ChatResponse;
import dev.langchain4j.model.chat.response.StreamingChatResponseHandler;
import dev.langchain4j.service.AiServices;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CompletableFuture;

import static org.assertj.core.api.Assertions.*;

class ListenerLifecycleTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider sdk = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final LangChain4jListener listener = new LangChain4jListener(new FITracer(sdk.get("test")), "synthetic");
    private final ChatRequest request = ChatRequest.builder().messages(UserMessage.from("private input")).build();

    @AfterEach void close() { sdk.close(); }

    @Test void cancellationFinishesSpanWithoutOnError() {
        CompletableFuture<ChatResponse> pending = new CompletableFuture<>();
        ChatModel model = new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public CompletableFuture<ChatResponse> doChatAsync(ChatRequest request) { return pending; }
        };
        ChatModel guarded = LangChain4jListener.guardLifecycle(ChatModel.class, model);
        CompletableFuture<ChatResponse> returned = guarded.chatAsync(request);
        assertThat(returned.cancel(false)).isTrue();
        assertThat(pending.isCancelled()).isTrue();
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(AttributeKey.stringKey("error.type")))
                    .isEqualTo("java.util.concurrent.CancellationException");
        });
        pending.completeExceptionally(new IllegalStateException("late failure"));
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test void ordinaryCompletionPreservesFutureIdentityAndExportsExactlyOnce() {
        CompletableFuture<ChatResponse> pending = new CompletableFuture<>();
        ChatModel model = LangChain4jListener.guardLifecycle(ChatModel.class, new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public CompletableFuture<ChatResponse> doChatAsync(ChatRequest request) { return pending; }
        });
        CompletableFuture<ChatResponse> returned = model.chatAsync(request);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        ChatResponse response = ChatResponse.builder().aiMessage(AiMessage.from("synthetic")).build();
        pending.complete(response);
        assertThat(returned.join()).isSameAs(response);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span ->
                assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK));
    }

    interface Assistant { String chat(String question); }

    @Test void guardedModelWorksInsideAiServicesWithoutDuplicateSpans() {
        ChatModel guarded = LangChain4jListener.guardLifecycle(ChatModel.class, new ChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public ChatResponse doChat(ChatRequest request) {
                return ChatResponse.builder().aiMessage(AiMessage.from("synthetic answer")).build();
            }
        });
        Assistant assistant = AiServices.builder(Assistant.class).chatModel(guarded).build();
        assertThat(assistant.chat("synthetic question")).isEqualTo("synthetic answer");
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span ->
                assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK));
    }

    @Test void synchronousStreamingSetupFailureFinishesSpanWithoutOnError() {
        IllegalStateException failure = new IllegalStateException("private provider failure");
        StreamingChatModel model = new StreamingChatModel() {
            @Override public List<ChatModelListener> listeners() { return List.of(listener); }
            @Override public void doChat(ChatRequest request, StreamingChatResponseHandler handler) {
                throw failure;
            }
        };
        StreamingChatModel guarded = LangChain4jListener.guardLifecycle(StreamingChatModel.class, model);
        assertThatThrownBy(() -> guarded.chat(request, new StreamingChatResponseHandler() {
            @Override public void onPartialResponse(String part) {}
            @Override public void onCompleteResponse(ChatResponse response) {}
            @Override public void onError(Throwable error) {}
        })).isSameAs(failure);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().toString() + span.getEvents()).doesNotContain("private provider failure");
        });
    }
}
