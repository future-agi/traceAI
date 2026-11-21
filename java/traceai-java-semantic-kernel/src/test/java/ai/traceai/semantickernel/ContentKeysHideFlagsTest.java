package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import com.microsoft.semantickernel.plugin.KernelPluginFactory;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction;
import com.microsoft.semantickernel.semanticfunctions.annotations.KernelFunctionParameter;
import com.microsoft.semantickernel.services.chatcompletion.AuthorRole;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;
import com.microsoft.semantickernel.services.chatcompletion.StreamingChatContent;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * Offline check that every content path the Semantic Kernel wrappers write (chat input and output
 * messages, streaming output, kernel function arguments and results, raw input/output, embedding text)
 * stays off exported spans under the hide flags. No network: the SDK services are stubbed.
 */
class ContentKeysHideFlagsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String IN = "SENTINEL-SK-INPUT-DO-NOT-EXPORT";
    private static final String OUT = "SENTINEL-SK-OUTPUT-DO-NOT-EXPORT";
    private static final String ARG = "SENTINEL-SK-ARGUMENT-DO-NOT-EXPORT";
    private static final String RES = "SENTINEL-SK-RESULT-DO-NOT-EXPORT";

    private static FITracer tracer(TraceConfig config) {
        otel.clearSpans();
        return new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
    }

    /** Every exported attribute key and value, and every event attribute, as one searchable string. */
    private static String everything() {
        StringBuilder sb = new StringBuilder();
        for (SpanData span : otel.getSpans()) {
            span.getAttributes().forEach((k, v) -> sb.append(k.getKey()).append('=').append(v).append('\n'));
            span.getEvents().forEach(e -> {
                sb.append(e.getName()).append('\n');
                e.getAttributes().forEach((k, v) -> sb.append(k.getKey()).append('=').append(v).append('\n'));
            });
        }
        return sb.toString();
    }

    private static List<TraceConfig> hideInputSide() {
        return List.of(TraceConfig.builder().hideInputs(true).build(),
            TraceConfig.builder().hideInputMessages(true).build());
    }

    private static List<TraceConfig> hideOutputSide() {
        return List.of(TraceConfig.builder().hideOutputs(true).build(),
            TraceConfig.builder().hideOutputMessages(true).build());
    }

    // ---- chat completion ----

    private static ChatCompletionService chatStub() {
        ChatCompletionService chat = mock(ChatCompletionService.class);
        List<ChatMessageContent<?>> reply = new ArrayList<>();
        reply.add(new ChatMessageContent<>(AuthorRole.ASSISTANT, OUT));
        when(chat.getChatMessageContentsAsync(any(ChatHistory.class), any(), any())).thenReturn(Mono.just(reply));
        StreamingChatContent<?> chunk = mock(StreamingChatContent.class);
        when(chunk.getContent()).thenReturn(OUT);
        when(chat.getStreamingChatMessageContentsAsync(any(ChatHistory.class), any(), any()))
            .thenReturn(Flux.<StreamingChatContent<?>>just(chunk));
        return chat;
    }

    private static void chat(TraceConfig config) {
        FITracer tracer = tracer(config);
        ChatHistory history = new ChatHistory();
        history.addSystemMessage("be brief");
        history.addUserMessage(IN);
        new TracedChatCompletionService(chatStub(), "gpt-test", "test", tracer)
            .getChatMessageContentsAsync(history, null, (InvocationContext) null).block();
    }

    private static void streamingChat(TraceConfig config) {
        FITracer tracer = tracer(config);
        ChatHistory history = new ChatHistory();
        history.addUserMessage(IN);
        new TracedChatCompletionService(chatStub(), "gpt-test", "test", tracer)
            .getStreamingChatMessageContentsAsync(history, null, (InvocationContext) null).collectList().block();
    }

    @Test
    void chatContentIsExportedWhenNothingIsHidden() {
        chat(TraceConfig.builder().build());
        assertThat(everything()).contains(IN, OUT);
    }

    @Test
    void chatInputMessagesAreHiddenByInputFlags() {
        for (TraceConfig config : hideInputSide()) {
            chat(config);
            assertThat(everything()).doesNotContain(IN);
        }
    }

    @Test
    void chatOutputMessagesAreHiddenByOutputFlags() {
        for (TraceConfig config : hideOutputSide()) {
            chat(config);
            assertThat(everything()).doesNotContain(OUT);
        }
    }

    @Test
    void streamingChatInputAndOutputAreHidden() {
        streamingChat(TraceConfig.builder().build());
        assertThat(everything()).contains(IN, OUT);
        for (TraceConfig config : hideInputSide()) {
            streamingChat(config);
            assertThat(everything()).doesNotContain(IN);
        }
        for (TraceConfig config : hideOutputSide()) {
            streamingChat(config);
            assertThat(everything()).doesNotContain(OUT);
        }
    }

    // ---- kernel functions (arguments are tool arguments, the result is the tool result) ----

    public static class EchoPlugin {
        @DefineKernelFunction(name = "echo", returnType = "java.lang.String")
        public String echo(@KernelFunctionParameter(name = "question") String question) {
            return RES;
        }
    }

    private static void kernelFunction(TraceConfig config) {
        FITracer tracer = tracer(config);
        var plugin = KernelPluginFactory.createFromObject(new EchoPlugin(), "local");
        Kernel kernel = Kernel.builder().withPlugin(plugin).build();
        KernelFunction<String> function = plugin.get("echo");
        KernelFunctionArguments arguments = KernelFunctionArguments.builder().withVariable("question", ARG).build();
        new TracedKernel(kernel, tracer).invokeAsync(function, arguments).block();
    }

    @Test
    void kernelFunctionContentIsExportedWhenNothingIsHidden() {
        kernelFunction(TraceConfig.builder().build());
        assertThat(everything()).contains(ARG, RES);
    }

    @Test
    void kernelFunctionArgumentsAreHiddenByInputFlags() {
        for (TraceConfig config : hideInputSide()) {
            kernelFunction(config);
            assertThat(everything()).doesNotContain(ARG);
        }
    }

    @Test
    void kernelFunctionResultIsHiddenByOutputFlags() {
        for (TraceConfig config : hideOutputSide()) {
            kernelFunction(config);
            assertThat(everything()).doesNotContain(RES);
        }
    }

    @Test
    void kernelPromptTextIsHiddenByInputFlags() {
        for (TraceConfig config : hideInputSide()) {
            FITracer tracer = tracer(config);
            Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, chatStub()).build();
            new TracedKernel(kernel, tracer).invokePromptAsync(IN).onErrorResume(e -> Mono.empty()).block();
            assertThat(everything()).doesNotContain(IN);
        }
    }

    // ---- embeddings ----

    private static void embed(TraceConfig config, boolean single) {
        FITracer tracer = tracer(config);
        TextEmbeddingGenerationService embedding = mock(TextEmbeddingGenerationService.class);
        when(embedding.generateEmbeddingsAsync(any())).thenReturn(Mono.just(List.of()));
        when(embedding.generateEmbeddingAsync(any())).thenReturn(Mono.empty());
        TracedTextEmbeddingGenerationService traced =
            new TracedTextEmbeddingGenerationService(embedding, "embedding-model", "test", tracer);
        if (single) {
            traced.generateEmbeddingAsync(IN).block();
        } else {
            traced.generateEmbeddingsAsync(List.of(IN)).block();
        }
    }

    @Test
    void embeddingTextIsHiddenByInputFlagsOnBothEntryPoints() {
        embed(TraceConfig.builder().build(), false);
        assertThat(everything()).contains(IN);
        for (TraceConfig config : hideInputSide()) {
            embed(config, false);
            assertThat(everything()).doesNotContain(IN);
            embed(config, true);
            assertThat(everything()).doesNotContain(IN);
        }
    }
}
