package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.plugin.KernelPlugin;
import com.microsoft.semantickernel.plugin.KernelPluginFactory;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import reactor.core.Disposable;
import reactor.core.publisher.Mono;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Runs the wrappers against real Semantic Kernel code and an in-process SDK HTTP transport. The global
 * OpenTelemetry instance is registered as {@code TraceAI.init} does, so Semantic Kernel's default
 * telemetry is exported too and any duplicate producer is visible.
 */
class NativeTelemetryTest {

    private static final AttributeKey<Boolean> CANCELLED = AttributeKey.booleanKey("semantic_kernel.cancelled");
    private static final AttributeKey<String> FI_KIND = AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND);
    private static final AttributeKey<Long> INPUT_TOKENS =
        AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
    private static final AttributeKey<Long> OUTPUT_TOKENS =
        AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
    private static final AttributeKey<Long> TOTAL_TOKENS =
        AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL);

    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private LocalOpenAi openAi;
    private Scope rootScope;

    @BeforeEach
    void setUp() throws Exception {
        GlobalOpenTelemetry.resetForTest();
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        openAi = new LocalOpenAi();
        rootScope = Context.root().makeCurrent();
    }

    @AfterEach
    void tearDown() {
        openAi.close();
        provider.close();
        rootScope.close();
        GlobalOpenTelemetry.resetForTest();
    }

    private FITracer tracer(TraceConfig config) {
        return new FITracer(provider.get("traceai-test"), config);
    }

    private FITracer tracer() {
        return tracer(TraceConfig.builder().build());
    }

    @Test
    void kernelPromptHasOneFunctionSpanAndOneModelSpanWithTokensOnce() {
        Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, openAi.chatService()).build();

        var result = new TracedKernel(kernel, tracer()).invokePromptAsync("Say hello").block();

        assertThat(result.getResult()).isEqualTo(LocalOpenAi.ANSWER);
        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(names(spans)).noneMatch(name -> name.startsWith("function_invocation"));
        SpanData agent = only(spans, "AGENT");
        SpanData llm = only(spans, "LLM");
        assertThat(spans).hasSize(2);
        assertThat(llm.getParentSpanId()).isEqualTo(agent.getSpanId());
        assertThat(llm.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM))).isEqualTo("openai");
        assertThat(llm.getAttributes().get(INPUT_TOKENS)).isEqualTo(7L);
        assertThat(llm.getAttributes().get(OUTPUT_TOKENS)).isEqualTo(3L);
        assertThat(llm.getAttributes().get(TOTAL_TOKENS)).isEqualTo(10L);
        assertThat(agent.getAttributes().get(INPUT_TOKENS)).isNull();
        assertThat(agent.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo(LocalOpenAi.ANSWER);
    }

    @Test
    void nativeChatOwnsTheModelCallAndWrapperRetainsContent() {
        TracedChatCompletionService chat =
            new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer());
        ChatHistory history = new ChatHistory();
        history.addUserMessage("Say hello");

        var messages = chat.getChatMessageContentsAsync(history, null, null).block();

        assertThat(messages.get(0).getContent()).isEqualTo(LocalOpenAi.ANSWER);
        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(2);
        SpanData llm = only(spans, "LLM");
        SpanData chain = only(spans, "CHAIN");
        assertThat(llm.getParentSpanId()).isEqualTo(chain.getSpanId());
        assertThat(chain.getAttributes().get(INPUT_TOKENS)).isNull();
        assertThat(llm.getAttributes().get(INPUT_TOKENS)).isEqualTo(7L);
        assertThat(llm.getAttributes().get(OUTPUT_TOKENS)).isEqualTo(3L);
        assertThat(llm.getAttributes().get(TOTAL_TOKENS)).isEqualTo(10L);
        assertThat(chain.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES)))
            .contains(LocalOpenAi.ANSWER);
    }

    @Test
    void kernelThroughChatWrapperHasNoDuplicateModelSpan() {
        FITracer tracer = tracer();
        Kernel kernel = Kernel.builder()
            .withAIService(ChatCompletionService.class,
                new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer))
            .build();

        new TracedKernel(kernel, tracer).invokePromptAsync("Say hello").block();

        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(3);
        SpanData agent = only(spans, "AGENT");
        SpanData llm = only(spans, "LLM");
        SpanData chain = only(spans, "CHAIN");
        assertThat(llm.getName()).isEqualTo("chat.completions gpt-local");
        assertThat(llm.getParentSpanId()).isEqualTo(chain.getSpanId());
        assertThat(chain.getParentSpanId()).isEqualTo(agent.getSpanId());
        assertThat(spans.stream().filter(span -> span.getAttributes().get(INPUT_TOKENS) != null)).hasSize(1);
    }

    @Test
    void callerContextIsRestoredAfterKernelInvocation() {
        KernelPlugin plugin = KernelPluginFactory.createFromObject(new DelayedPlugin(), "local");
        Kernel kernel = Kernel.builder().withPlugin(plugin).build();
        TracedKernel traced = new TracedKernel(kernel, tracer());

        traced.invokeAsync(plugin.<String>get("delayed")).block();
        assertThat(Span.current().getSpanContext().isValid()).isFalse();

        traced.invokeAsync(plugin.<String>get("immediate")).block();
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
        assertThat(names(exporter.getFinishedSpanItems())).noneMatch(name -> name.startsWith("function_invocation"));
    }

    @Test
    void reactiveFunctionCodeAnnotatesTheWrapperSpan() {
        KernelPlugin plugin = KernelPluginFactory.createFromObject(new DelayedPlugin(), "local");
        Kernel kernel = Kernel.builder().withPlugin(plugin).build();

        new TracedKernel(kernel, tracer()).invokeAsync(plugin.<String>get("annotated")).block();

        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getAttributes().get(AttributeKey.stringKey("app.note"))).isEqualTo("from-function");
        assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void cancellingAKernelInvocationEndsTheModelSpan() throws Exception {
        openAi.mode(LocalOpenAi.Mode.HANG);
        Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, openAi.chatService()).build();

        Disposable subscription = new TracedKernel(kernel, tracer()).invokePromptAsync("Say hello").subscribe();
        assertThat(openAi.received.await(10, TimeUnit.SECONDS)).isTrue();
        subscription.dispose();

        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(2);
        assertThat(spans).allSatisfy(span -> {
            assertThat(span.getAttributes().get(CANCELLED)).isTrue();
            assertThat(span.getStatus().getStatusCode()).isNotEqualTo(StatusCode.OK);
        });
    }

    @Test
    void hiddenErrorDetailsAreNotRecordedOnSemanticKernelSpans() {
        openAi.mode(LocalOpenAi.Mode.SERVER_ERROR);
        Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, openAi.chatService()).build();
        TracedKernel traced = new TracedKernel(kernel, tracer(TraceConfig.builder().hideErrorDetails(true).build()));

        assertThatThrownBy(() -> traced.invokePromptAsync("Say hello").block())
            .hasMessageContaining(LocalOpenAi.ERROR_DETAIL);

        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).isNotEmpty();
        assertThat(spans.toString()).doesNotContain(LocalOpenAi.ERROR_DETAIL);
        assertThat(spans).allSatisfy(span ->
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR));
    }

    @Test
    void kernelInvocationIsLazyAndEachSubscriptionIsTracedSeparately() {
        Kernel kernel = Kernel.builder().withAIService(ChatCompletionService.class, openAi.chatService()).build();
        KernelFunction<String> function = KernelFunction.<String>createFromPrompt("Say hello").build();
        var result = new TracedKernel(kernel, tracer()).invokeAsync(function);

        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        assertThat(openAi.requests.get()).isZero();

        result.block();
        result.block();

        assertThat(openAi.requests.get()).isEqualTo(2);
        List<SpanData> agents = exporter.getFinishedSpanItems().stream()
            .filter(span -> "AGENT".equals(span.getAttributes().get(FI_KIND)))
            .collect(Collectors.toList());
        assertThat(agents).hasSize(2);
        assertThat(agents.get(0).getTraceId()).isNotEqualTo(agents.get(1).getTraceId());
    }

    @Test
    void nativeModelRoundsAreCountedEvenWhenOnlyLastMessageIsReturned() {
        openAi.mode(LocalOpenAi.Mode.TOOL);
        Kernel kernel = Kernel.builder()
            .withPlugin(KernelPluginFactory.createFromObject(new DelayedPlugin(), "local")).build();
        var context = com.microsoft.semantickernel.orchestration.InvocationContext.builder()
            .withToolCallBehavior(com.microsoft.semantickernel.orchestration.ToolCallBehavior.allowAllKernelFunctions(true))
            .withReturnMode(com.microsoft.semantickernel.orchestration.InvocationReturnMode.LAST_MESSAGE_ONLY)
            .build();
        var chat = new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer());
        var result = chat.getChatMessageContentsAsync("Say hello", kernel, context).block();
        assertThat(result).hasSize(1);
        assertThat(result.get(0).getContent()).isEqualTo(LocalOpenAi.ANSWER);
        assertThat(openAi.requests).hasValue(2);
        var models = exporter.getFinishedSpanItems().stream()
            .filter(span -> "LLM".equals(span.getAttributes().get(FI_KIND)))
            .collect(Collectors.toList());
        assertThat(models).hasSize(2);
        assertThat(models.stream().mapToLong(span -> span.getAttributes().get(TOTAL_TOKENS)).sum()).isEqualTo(20);
        assertThat(exporter.getFinishedSpanItems().stream()
            .filter(span -> !"LLM".equals(span.getAttributes().get(FI_KIND))))
            .allSatisfy(span -> assertThat(span.getAttributes().get(INPUT_TOKENS)).isNull());
    }

    @Test
    void automaticToolCallIsOneTraceAiToolSpanWithHiddenErrorDetails() {
        DelayedPlugin plugin = new DelayedPlugin();
        openAi.mode(LocalOpenAi.Mode.TOOL).tool("local-failing");

        // Semantic Kernel reports the tool failure to the model and asks again.
        List<SpanData> spans = runToolRound(plugin, tracer(TraceConfig.builder().hideErrorDetails(true).build()))
            .getFinishedSpanItems();

        assertThat(plugin.failingCalls).hasValue(1);
        SpanData chain = only(spans, "CHAIN");
        SpanData tool = onlyFunctionSpan(spans);
        assertThat(tool.getAttributes().get(FI_KIND)).isEqualTo("TOOL");
        assertThat(tool.getTraceId()).isEqualTo(chain.getTraceId());
        assertThat(spanIds(spans)).contains(tool.getParentSpanId());
        assertThat(tool.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(tool.getStatus().getDescription()).isEmpty();
        assertThat(tool.getEvents()).allSatisfy(event -> {
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.message"))).isNull();
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.stacktrace"))).isNull();
        });
        assertThat(tool.toString()).doesNotContain(LocalOpenAi.ERROR_DETAIL);
    }

    @Test
    void automaticToolCallUsesTheTraceAiTracerWithoutGlobalTelemetry() {
        // A tool span that only appeared through GlobalOpenTelemetry would vanish here.
        GlobalOpenTelemetry.resetForTest();
        DelayedPlugin plugin = new DelayedPlugin();
        openAi.mode(LocalOpenAi.Mode.TOOL);

        List<SpanData> spans = runToolRound(plugin, tracer()).getFinishedSpanItems();

        SpanData tool = onlyFunctionSpan(spans);
        assertThat(tool.getName()).isEqualTo("function_invocation local-immediate");
        assertThat(tool.getAttributes().get(FI_KIND)).isEqualTo("TOOL");
        assertThat(tool.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(tool.getTraceId()).isEqualTo(only(spans, "CHAIN").getTraceId());
        assertThat(spans.stream().filter(span -> "LLM".equals(span.getAttributes().get(FI_KIND)))).hasSize(2);
    }

    @Test
    void cancellingDuringAnAutomaticToolCallEndsTheToolSpan() throws Exception {
        DelayedPlugin plugin = new DelayedPlugin();
        openAi.mode(LocalOpenAi.Mode.TOOL).tool("local-hanging");
        Kernel kernel = Kernel.builder().withPlugin(KernelPluginFactory.createFromObject(plugin, "local")).build();
        var chat = new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer());

        Disposable subscription = chat.getChatMessageContentsAsync("Say hello", kernel, autoInvoke()).subscribe();
        assertThat(plugin.hangingStarted.await(10, TimeUnit.SECONDS)).isTrue();
        subscription.dispose();

        SpanData tool = onlyFunctionSpan(exporter.getFinishedSpanItems());
        assertThat(tool.getAttributes().get(FI_KIND)).isEqualTo("TOOL");
        assertThat(tool.getAttributes().get(CANCELLED)).isTrue();
        assertThat(tool.getStatus().getStatusCode()).isNotEqualTo(StatusCode.OK);
    }

    @Test
    void userPreToolCallHooksStillRunBeforeTheToolIsTraced() {
        DelayedPlugin plugin = new DelayedPlugin();
        openAi.mode(LocalOpenAi.Mode.TOOL).tool("local-failing");
        Kernel kernel = Kernel.builder().withPlugin(KernelPluginFactory.createFromObject(plugin, "local")).build();
        KernelFunction<?> replacement = kernel.getFunction("local", "immediate");
        var hooks = new com.microsoft.semantickernel.hooks.KernelHooks();
        hooks.addPreToolCallHook(event -> new com.microsoft.semantickernel.hooks.PreToolCallEvent(
            replacement.getName(), event.getArguments(), replacement,
            new com.microsoft.semantickernel.contextvariables.ContextVariableTypes()));
        var context = com.microsoft.semantickernel.orchestration.InvocationContext.builder()
            .withToolCallBehavior(com.microsoft.semantickernel.orchestration.ToolCallBehavior.allowAllKernelFunctions(true))
            .withKernelHooks(hooks)
            .build();
        var chat = new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer());

        chat.getChatMessageContentsAsync("Say hello", kernel, context).block();

        assertThat(plugin.failingCalls).hasValue(0);
        SpanData tool = onlyFunctionSpan(exporter.getFinishedSpanItems());
        assertThat(tool.getName()).isEqualTo("function_invocation local-immediate");
        assertThat(tool.getAttributes().get(FI_KIND)).isEqualTo("TOOL");
    }

    private InMemorySpanExporter runToolRound(DelayedPlugin plugin, FITracer tracer) {
        Kernel kernel = Kernel.builder().withPlugin(KernelPluginFactory.createFromObject(plugin, "local")).build();
        var chat = new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer);
        var result = chat.getChatMessageContentsAsync("Say hello", kernel, autoInvoke()).block();
        assertThat(result).isNotEmpty();
        assertThat(openAi.requests).hasValue(2);
        return exporter;
    }

    private static com.microsoft.semantickernel.orchestration.InvocationContext autoInvoke() {
        return com.microsoft.semantickernel.orchestration.InvocationContext.builder()
            .withToolCallBehavior(com.microsoft.semantickernel.orchestration.ToolCallBehavior.allowAllKernelFunctions(true))
            .build();
    }

    private static SpanData onlyFunctionSpan(List<SpanData> spans) {
        List<SpanData> functions = spans.stream()
            .filter(span -> span.getName().startsWith("function_invocation"))
            .collect(Collectors.toList());
        assertThat(functions).as("function spans in %s", names(spans)).hasSize(1);
        return functions.get(0);
    }

    private static List<String> spanIds(List<SpanData> spans) {
        return spans.stream().map(SpanData::getSpanId).collect(Collectors.toList());
    }

    @Test
    void realStreamingServicePreservesContentWithoutInventingUsage() {
        openAi.mode(LocalOpenAi.Mode.STREAM);
        var chat = new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer());
        var chunks = chat.getStreamingChatMessageContentsAsync("Say hello", null, null).collectList().block();
        assertThat(chunks).isNotEmpty();
        assertThat(chunks.stream().map(chunk -> chunk.getContent()).collect(Collectors.joining()))
            .isEqualTo(LocalOpenAi.ANSWER);
        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        assertThat(only(spans, "LLM").getAttributes().get(INPUT_TOKENS)).isNull();
        assertThat(spans.get(0).getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo(LocalOpenAi.ANSWER);
    }

    @Test
    void realStreamingCancellationEndsSpanPromptly() throws Exception {
        openAi.mode(LocalOpenAi.Mode.HANG);
        var chat = new TracedChatCompletionService(openAi.chatService(), "gpt-local", "openai", tracer());
        var subscription = chat.getStreamingChatMessageContentsAsync("Say hello", null, null).subscribe();
        assertThat(openAi.received.await(10, TimeUnit.SECONDS)).isTrue();
        subscription.dispose();
        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getAttributes().get(CANCELLED)).isTrue();
        assertThat(spans.get(0).getStatus().getStatusCode()).isNotEqualTo(StatusCode.OK);
    }

    private static List<String> names(List<SpanData> spans) {
        return spans.stream().map(SpanData::getName).collect(Collectors.toList());
    }

    private static SpanData only(List<SpanData> spans, String kind) {
        List<SpanData> matching = spans.stream()
            .filter(span -> kind.equals(span.getAttributes().get(FI_KIND)))
            .collect(Collectors.toList());
        assertThat(matching).as("%s spans in %s", kind, names(spans)).hasSize(1);
        return matching.get(0);
    }

    public static class DelayedPlugin {
        final java.util.concurrent.atomic.AtomicInteger failingCalls = new java.util.concurrent.atomic.AtomicInteger();
        final java.util.concurrent.CountDownLatch hangingStarted = new java.util.concurrent.CountDownLatch(1);

        @DefineKernelFunction(name = "failing", returnType = "java.lang.String")
        public String failing() {
            failingCalls.incrementAndGet();
            throw new IllegalStateException(LocalOpenAi.ERROR_DETAIL);
        }

        @DefineKernelFunction(name = "hanging", returnType = "java.lang.String")
        public Mono<String> hanging() {
            return Mono.<String>never().doOnSubscribe(ignored -> hangingStarted.countDown());
        }

        @DefineKernelFunction(name = "delayed", returnType = "java.lang.String")
        public Mono<String> delayed() {
            return Mono.just("later").delayElement(Duration.ofMillis(20));
        }

        @DefineKernelFunction(name = "annotated", returnType = "java.lang.String")
        public Mono<String> annotated() {
            return Mono.fromSupplier(() -> {
                Span.current().setAttribute("app.note", "from-function");
                return "annotated";
            });
        }

        @DefineKernelFunction(name = "immediate", returnType = "java.lang.String")
        public String immediate() {
            return "now";
        }
    }
}
