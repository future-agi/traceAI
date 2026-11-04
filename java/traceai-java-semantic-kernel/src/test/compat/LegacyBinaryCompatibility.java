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
        AtomicInteger started = new AtomicInteger();
        try (SdkTracerProvider sdk = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter))
                .addSpanProcessor(new io.opentelemetry.sdk.trace.SpanProcessor() {
                    public void onStart(io.opentelemetry.context.Context parent, io.opentelemetry.sdk.trace.ReadWriteSpan span) {
                        started.incrementAndGet();
                    }
                    public boolean isStartRequired() { return true; }
                    public void onEnd(io.opentelemetry.sdk.trace.ReadableSpan span) { }
                    public boolean isEndRequired() { return false; }
                }).build()) {
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
                var echoPlugin = new EchoPlugin();
                var plugin = com.microsoft.semantickernel.plugin.KernelPluginFactory.createFromObject(
                    echoPlugin, "local");
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

                exporter.reset();
                int beforeStarts = started.get();
                int beforeCalls = echoPlugin.calls.get();
                java.util.Map<String, Object> cycle = new java.util.LinkedHashMap<>();
                cycle.put("self", cycle);
                arguments.put("unused", new com.microsoft.semantickernel.contextvariables.ContextVariable<>(
                    new com.microsoft.semantickernel.contextvariables.ContextVariableType<>(
                        new com.microsoft.semantickernel.contextvariables.ContextVariableTypeConverter.NoopConverter<>(java.util.Map.class),
                        java.util.Map.class), cycle));
                control = inputKernel.invokeAsync(echo).withArguments(arguments).block().getResult();
                actual = new TracedKernel(inputKernel, inputTracer).invokeAsync(echo, arguments).block().getResult();
                if (!"received: synthetic-legacy-question".equals(control) || !control.equals(actual))
                    throw new AssertionError("cyclic input changed the business result");
                if (echoPlugin.calls.get() - beforeCalls != 2) throw new AssertionError("cyclic input prevented execution");
                var cycleSpans = exporter.getFinishedSpanItems();
                if (started.get() - beforeStarts != 1 || cycleSpans.size() != 1 || !cycleSpans.get(0).hasEnded())
                    throw new AssertionError("cyclic input leaked a span");
                var raw = com.google.gson.JsonParser.parseString(cycleSpans.get(0).getAttributes().get(
                    io.opentelemetry.api.common.AttributeKey.stringKey("fi.raw_input"))).getAsJsonObject();
                if (!"synthetic-legacy-question".equals(raw.get("question").getAsString())
                        || !"[circular]".equals(raw.getAsJsonObject("unused").get("self").getAsString()))
                    throw new AssertionError("cyclic input not safely captured");
                if (cycle.get("self") != cycle) throw new AssertionError("cyclic argument mutated");
                System.out.println("PASS: cyclic input, identical result, 2 executions, 1 started/ended span");
                verifyCyclicOutput(sdk, exporter, started);
            } catch (Throwable failure) {
                failure.printStackTrace();
                System.exit(12);
            }
        }
    }

    private static void verifyCyclicOutput(SdkTracerProvider sdk, InMemorySpanExporter exporter,
            AtomicInteger started) throws InterruptedException {
        for (boolean hideOutputs : new boolean[] {false, true}) {
            exporter.reset();
            int beforeStarts = started.get();
            var delegate = new OutputPlugin();
            java.util.Map<String, Object> first = new java.util.LinkedHashMap<>();
            java.util.Map<String, Object> second = new java.util.LinkedHashMap<>();
            first.put("private", "synthetic-output-secret");
            first.put("next", second);
            second.put("next", first);
            delegate.value = first;
            var plugin = com.microsoft.semantickernel.plugin.KernelPluginFactory.createFromObject(delegate, "output");
            Kernel kernel = Kernel.builder().withPlugin(plugin).build();
            KernelFunction<java.util.Map<String, Object>> function = plugin.get("result");
            var control = kernel.invokeAsync(function).block(java.time.Duration.ofSeconds(5));
            if (control.getResult() != first) throw new AssertionError("untraced result changed");
            var tracer = new FITracer(sdk.get("offline-legacy-output"),
                TraceConfig.builder().hideOutputs(hideOutputs).build());
            var completed = new java.util.concurrent.CountDownLatch(1);
            var received = new java.util.concurrent.atomic.AtomicReference<Object>();
            var error = new java.util.concurrent.atomic.AtomicReference<Throwable>();
            var subscription = new TracedKernel(kernel, tracer).invokeAsync(function)
                .subscribeOn(reactor.core.scheduler.Schedulers.boundedElastic())
                .subscribe(value -> received.set(value.getResult()),
                    failure -> { error.set(failure); completed.countDown(); }, completed::countDown);
            try {
                boolean done = completed.await(5, java.util.concurrent.TimeUnit.SECONDS);
                var spans = exporter.getFinishedSpanItems();
                // Check before cancellation; cancellation itself could close a leaked span.
                if (started.get() - beforeStarts != 1 || spans.size() != 1 || !spans.get(0).hasEnded())
                    throw new AssertionError("cyclic output leaked a span, hideOutputs=" + hideOutputs);
                if (!done || error.get() != null || received.get() != first || delegate.calls.get() != 2)
                    throw new AssertionError("cyclic output changed the invocation, hideOutputs=" + hideOutputs);
                if (first.get("next") != second || second.get("next") != first)
                    throw new AssertionError("cyclic output mutated");
                var span = spans.get(0);
                if (span.getStatus().getStatusCode() != io.opentelemetry.api.trace.StatusCode.OK
                        || !span.getEvents().isEmpty()
                        || !"AGENT".equals(span.getAttributes().get(
                            io.opentelemetry.api.common.AttributeKey.stringKey("fi.span.kind"))))
                    throw new AssertionError("cyclic output did not end as a successful AGENT span");
                String readable = span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("output.value"));
                String raw = span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("fi.raw_output"));
                if (hideOutputs) {
                    if (readable != null || raw != null || spans.toString().contains("synthetic-output-secret"))
                        throw new AssertionError("hidden output leaked");
                } else {
                    if (readable == null || !readable.contains("[circular]"))
                        throw new AssertionError("cyclic output not safely formatted");
                    var json = com.google.gson.JsonParser.parseString(raw).getAsJsonObject();
                    if (!"[circular]".equals(json.getAsJsonObject("next").get("next").getAsString()))
                        throw new AssertionError("cyclic output not safely serialized");
                }
                System.out.println("PASS: cyclic output, original result, 2 executions, 1 started/ended span, hideOutputs=" + hideOutputs);
            } finally {
                subscription.dispose();
            }
        }
    }

    public static class OutputPlugin {
        final AtomicInteger calls = new AtomicInteger();
        java.util.Map<String, Object> value;
        @com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction(
            name = "result", returnType = "java.util.Map")
        public java.util.Map<String, Object> result() {
            calls.incrementAndGet();
            return value;
        }
    }

    public static class EchoPlugin {
        final AtomicInteger calls = new AtomicInteger();
        @com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction(
            name = "echo", returnType = "java.lang.String")
        public String echo(
                @com.microsoft.semantickernel.semanticfunctions.annotations.KernelFunctionParameter(name = "question")
                String question) {
            calls.incrementAndGet();
            return "received: " + question;
        }
    }
}
