import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import ai.traceai.semantickernel.TracedKernel;
import ai.traceai.semantickernel.TracedChatCompletionService;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;
import com.microsoft.semantickernel.services.chatcompletion.AuthorRole;
import com.microsoft.semantickernel.services.chatcompletion.StreamingChatContent;
import com.microsoft.semantickernel.orchestration.InvocationContext;
import com.microsoft.semantickernel.orchestration.PromptExecutionSettings;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

public class LegacyBinaryCompatibility {
    public static void main(String[] args) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider sdk = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            FITracer tracer = new FITracer(sdk.get("offline-legacy"), TraceConfig.builder().hideInputs(true).hideOutputs(true).build());
            AtomicInteger calls = new AtomicInteger();
            ChatCompletionService chat = new ChatCompletionService() {
                public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(ChatHistory history, Kernel kernel, InvocationContext context) {
                    return Mono.defer(() -> { calls.incrementAndGet(); return Mono.just(List.of(new ChatMessageContent<>(AuthorRole.ASSISTANT, "answer"))); });
                }
                public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(String prompt, Kernel kernel, InvocationContext context) {
                    return getChatMessageContentsAsync(new ChatHistory(), kernel, context);
                }
                public Flux<StreamingChatContent<?>> getStreamingChatMessageContentsAsync(ChatHistory history, Kernel kernel, InvocationContext context) { return Flux.empty(); }
                public Flux<StreamingChatContent<?>> getStreamingChatMessageContentsAsync(String prompt, Kernel kernel, InvocationContext context) { return Flux.empty(); }
                public String getModelId() { return "synthetic"; }
                public String getServiceId() { return "synthetic"; }
            };
            Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, new TracedChatCompletionService(chat, "synthetic", "custom", tracer)).build();
            TracedKernel traced = new TracedKernel(kernel, tracer);
            KernelFunction<String> function = KernelFunction.<String>createFromPrompt("hello").build();
            try {
                var result = traced.invokeAsync(function, KernelFunctionArguments.builder().build());
                if (calls.get() != 0) throw new AssertionError("early delegate call");
                if (!"answer".equals(result.block().getResult())) throw new AssertionError("wrong result");
                if (!"answer".equals(result.block().getResult())) throw new AssertionError("wrong resubscription result");
                if (!"answer".equals(traced.invokePromptAsync("hello").block().getResult())) throw new AssertionError("wrong prompt result");
                if (!"answer".equals(traced.invokeAsync(function).block().getResult())) throw new AssertionError("wrong default arguments");
                var context = InvocationContext.builder()
                    .withPromptExecutionSettings(PromptExecutionSettings.builder().build()).build();
                var wrappedChat = new TracedChatCompletionService(chat, "synthetic", "custom", tracer);
                if (wrappedChat.getChatMessageContentsAsync(new ChatHistory(), null, context).block().isEmpty())
                    throw new AssertionError("wrong direct chat result");
                wrappedChat.getStreamingChatMessageContentsAsync(new ChatHistory(), null, context).blockLast();
                if (calls.get() != 5) throw new AssertionError("calls=" + calls.get());
                var spans = exporter.getFinishedSpanItems();
                if (spans.size() != 10) throw new AssertionError("spans=" + spans);
                if (spans.toString().contains("hello") || spans.toString().contains("answer")) throw new AssertionError("content leaked");
                System.out.println("PASS: 6 subscriptions, 10 privacy-safe spans");

                exporter.reset();
                var plugin = com.microsoft.semantickernel.plugin.KernelPluginFactory.createFromObject(
                    new EchoPlugin(), "local");
                Kernel inputKernel = Kernel.builder().withPlugin(plugin).build();
                KernelFunction<String> echo = plugin.get("echo");
                var arguments = KernelFunctionArguments.builder()
                    .withVariable("question", "synthetic-legacy-question").build();
                String control = inputKernel.invokeAsync(echo).withArguments(arguments).block().getResult();
                var inputTracer = new FITracer(sdk.get("offline-legacy-input"), TraceConfig.builder().build());
                String actual = new TracedKernel(inputKernel, inputTracer)
                    .invokeAsync(echo, arguments).block().getResult();
                if (!"received: synthetic-legacy-question".equals(control) || !control.equals(actual))
                    throw new AssertionError("traced and untraced argument handling differ");
                var inputSpans = exporter.getFinishedSpanItems();
                if (inputSpans.size() != 1) throw new AssertionError("input spans=" + inputSpans);
                var attributes = inputSpans.get(0).getAttributes();
                if (!"question: synthetic-legacy-question".equals(attributes.get(
                        io.opentelemetry.api.common.AttributeKey.stringKey("input.value"))))
                    throw new AssertionError("argument value not captured");
                if (!"{\"question\":\"synthetic-legacy-question\"}".equals(attributes.get(
                        io.opentelemetry.api.common.AttributeKey.stringKey("fi.raw_input"))))
                    throw new AssertionError("raw arguments not flattened");
                System.out.println("PASS: identical traced/untraced result and flattened exported inputs");
            } catch (Throwable failure) {
                failure.printStackTrace();
                System.exit(12);
            }
        }
    }

    public static class EchoPlugin {
        @com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction(
            name = "echo", returnType = "java.lang.String")
        public String echo(
                @com.microsoft.semantickernel.semanticfunctions.annotations.KernelFunctionParameter(name = "question")
                String question) {
            return "received: " + question;
        }
    }
}
