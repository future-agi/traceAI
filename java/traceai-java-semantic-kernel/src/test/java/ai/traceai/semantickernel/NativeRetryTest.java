package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.plugin.KernelPluginFactory;
import com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction;
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
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.util.retry.Retry;

import static org.assertj.core.api.Assertions.assertThat;

/** Exercises native function resubscriptions, not just retries around the traceAI wrapper. */
class NativeRetryTest {
    private static final AttributeKey<String> KIND = AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND);
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private Scope rootScope;

    @BeforeEach
    void setUp() {
        GlobalOpenTelemetry.resetForTest();
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        OpenTelemetrySdk.builder().setTracerProvider(provider).buildAndRegisterGlobal();
        rootScope = Context.root().makeCurrent();
    }

    @AfterEach
    void tearDown() {
        provider.close();
        rootScope.close();
        GlobalOpenTelemetry.resetForTest();
    }

    private FITracer tracer() {
        return new FITracer(provider.get("traceai-test"), TraceConfig.builder().hideErrorDetails(true).build());
    }

    @ParameterizedTest
    @ValueSource(strings = {"serial", "serialSync", "parallel", "retried", "recovered"})
    void siblingCallsKeepTheirCallingAgentAsParent(String mode) {
        Kernel kernel = kernel(false);
        new TracedKernel(kernel, tracer()).invokeAsync(kernel.<String>getFunction("nested", mode)).block();

        List<SpanData> spans = exporter.getFinishedSpanItems();
        SpanData agent = spans.stream().filter(s -> "AGENT".equals(s.getAttributes().get(KIND)))
            .findFirst().orElseThrow();
        assertThat(spans).hasSize(3);
        assertThat(spans.stream().filter(s -> "TOOL".equals(s.getAttributes().get(KIND))))
            .hasSize(2).allSatisfy(s -> assertThat(s.getParentSpanId()).isEqualTo(agent.getSpanId()));
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @ParameterizedTest
    @CsvSource({"false,false", "false,true", "true,false", "true,true"})
    void retryAttemptsRemainSiblingsUnderTheirImmediateCaller(boolean nestedTool, boolean asyncFailure) {
        Kernel kernel = kernel(asyncFailure);
        Span caller = provider.get("caller").spanBuilder("caller").startSpan();
        try (Scope ignored = caller.makeCurrent()) {
            var result = new TracedKernel(kernel, tracer())
                .invokeAsync(kernel.<String>getFunction("nested", nestedTool ? "outer" : "retried")).block();
            assertThat(result.getResult()).isEqualTo("done");
            assertThat(Span.current().getSpanContext()).isEqualTo(caller.getSpanContext());
        }
        List<SpanData> spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(nestedTool ? 4 : 3);
        SpanData agent = spans.stream().filter(s -> "AGENT".equals(s.getAttributes().get(KIND)))
            .findFirst().orElseThrow();
        SpanData parent = nestedTool ? spans.stream()
            .filter(s -> "function_invocation nested-retried".equals(s.getName())).findFirst().orElseThrow() : agent;
        assertThat(agent.getParentSpanId()).isEqualTo(caller.getSpanContext().getSpanId());
        if (nestedTool) {
            assertThat(parent.getParentSpanId()).isEqualTo(agent.getSpanId());
        }
        List<SpanData> attempts = spans.stream()
            .filter(s -> "function_invocation nested-flaky".equals(s.getName())).toList();
        assertThat(attempts).hasSize(2).allSatisfy(s -> {
            assertThat(s.getParentSpanId()).isEqualTo(parent.getSpanId());
            assertThat(s.getTraceId()).isEqualTo(agent.getTraceId());
            assertThat(s.getStartEpochNanos()).isGreaterThanOrEqualTo(parent.getStartEpochNanos());
            assertThat(s.getEndEpochNanos()).isLessThanOrEqualTo(parent.getEndEpochNanos());
        });
        assertThat(attempts).extracting(s -> s.getStatus().getStatusCode())
            .containsExactly(StatusCode.ERROR, StatusCode.OK);
        assertThat(attempts.get(0).getSpanId()).isNotEqualTo(attempts.get(1).getSpanId());
        assertThat(spans.toString()).doesNotContain("synthetic-retry-error");
        caller.end();
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void retriesAroundTheWrapperKeepTheCaller(boolean external) {
        Kernel kernel = kernel(false);
        Span caller = external ? provider.get("caller").spanBuilder("caller").startSpan() : Span.getInvalid();
        try (Scope ignored = caller.makeCurrent()) {
            new TracedKernel(kernel, tracer()).invokeAsync(kernel.<String>getFunction("nested", "flaky"))
                .retry(1).block();
            assertThat(Span.current().getSpanContext()).isEqualTo(caller.getSpanContext());
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(2)
            .allSatisfy(s -> assertThat(s.getParentSpanId()).isEqualTo(caller.getSpanContext().getSpanId()));
        caller.end();
    }

    @ParameterizedTest
    @ValueSource(strings = {"blocking", "blockingSync", "blockingRetry", "blockingSyncRetry",
        "reactive", "retryDelayed", "repeat"})
    void nestedCallsKeepTheirImmediateCallerWithoutGlobalSdk(String mode) {
        GlobalOpenTelemetry.resetForTest();
        Kernel kernel = Kernel.builder()
            .withPlugin(KernelPluginFactory.createFromObject(new BlockingNested(mode), "blocking")).build();
        var result = new TracedKernel(kernel, tracer())
            .invokeAsync(kernel.<String>getFunction("blocking", "outer")).block();
        assertThat(result.getResult()).isEqualTo("done");

        List<SpanData> spans = exporter.getFinishedSpanItems();
        boolean retried = mode.endsWith("Retry") || mode.equals("retryDelayed");
        int leaves = retried || mode.equals("repeat") ? 2 : 1;
        assertThat(spans).hasSize(2 + leaves);
        SpanData agent = spans.stream().filter(s -> "AGENT".equals(s.getAttributes().get(KIND)))
            .findFirst().orElseThrow();
        String middleName = mode.startsWith("blockingSync") ? "middleSync" : "middle";
        SpanData middle = spans.stream()
            .filter(s -> s.getName().equals("function_invocation blocking-" + middleName))
            .findFirst().orElseThrow();
        assertThat(middle.getParentSpanId()).isEqualTo(agent.getSpanId());
        List<SpanData> attempts = spans.stream()
            .filter(s -> s.getName().equals("function_invocation blocking-leaf")).toList();
        assertThat(attempts).hasSize(leaves).allSatisfy(s -> {
            assertThat(s.getParentSpanId()).isEqualTo(middle.getSpanId());
            assertThat(s.getTraceId()).isEqualTo(agent.getTraceId());
            assertThat(s.getAttributes().get(KIND)).isEqualTo("TOOL");
            assertThat(s.getStartEpochNanos()).isGreaterThanOrEqualTo(middle.getStartEpochNanos());
            assertThat(s.getEndEpochNanos()).isLessThanOrEqualTo(middle.getEndEpochNanos());
        });
        if (retried) {
            assertThat(attempts).extracting(s -> s.getStatus().getStatusCode())
                .containsExactly(StatusCode.ERROR, StatusCode.OK);
        }
        assertThat(spans.toString()).doesNotContain("synthetic-blocking-error");
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    public static class BlockingNested {
        private final String mode;
        private final AtomicInteger calls = new AtomicInteger();

        BlockingNested(String mode) {
            this.mode = mode;
        }

        @DefineKernelFunction(name = "outer", returnType = "java.lang.String")
        public Mono<String> outer(Kernel kernel) {
            return kernel.<String>invokeAsync("blocking",
                mode.startsWith("blockingSync") ? "middleSync" : "middle").map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "middleSync", returnType = "java.lang.String")
        public String middleSync(Kernel kernel) {
            return kernel.<String>invokeAsync("blocking", "leaf")
                .retry(mode.endsWith("Retry") ? 1 : 0).block().getResult();
        }

        @DefineKernelFunction(name = "middle", returnType = "java.lang.String")
        public Mono<String> middle(Kernel kernel) {
            var call = kernel.<String>invokeAsync("blocking", "leaf");
            if (mode.startsWith("blocking")) {
                return Mono.just(call.retry(mode.endsWith("Retry") ? 1 : 0).block().getResult());
            }
            if (mode.equals("retryDelayed")) {
                return call.retryWhen(Retry.fixedDelay(1, Duration.ofMillis(5))).map(r -> r.getResult());
            }
            if (mode.equals("repeat")) {
                return call.repeat(1).last().map(r -> r.getResult());
            }
            return call.map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "leaf", returnType = "java.lang.String")
        public String leaf() {
            if ((mode.endsWith("Retry") || mode.equals("retryDelayed")) && calls.incrementAndGet() == 1) {
                throw new IllegalStateException("synthetic-blocking-error");
            }
            return "done";
        }
    }

    private Kernel kernel(boolean asyncFailure) {
        return Kernel.builder().withPlugin(KernelPluginFactory.createFromObject(new Nested(asyncFailure), "nested"))
            .build();
    }

    public static class Nested {
        private final AtomicInteger calls = new AtomicInteger();
        private final boolean asyncFailure;

        Nested(boolean asyncFailure) {
            this.asyncFailure = asyncFailure;
        }

        @DefineKernelFunction(name = "serial", returnType = "java.lang.String")
        public Mono<String> serial(Kernel kernel) {
            return kernel.<String>invokeAsync("nested", "leaf")
                .then(Mono.defer(() -> kernel.<String>invokeAsync("nested", "leaf"))).map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "serialSync", returnType = "java.lang.String")
        public Mono<String> serialSync(Kernel kernel) {
            return kernel.<String>invokeAsync("nested", "immediate")
                .then(Mono.defer(() -> kernel.<String>invokeAsync("nested", "immediate"))).map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "recovered", returnType = "java.lang.String")
        public Mono<String> recovered(Kernel kernel) {
            return kernel.<String>invokeAsync("nested", "flaky")
                .onErrorResume(error -> kernel.<String>invokeAsync("nested", "immediate")).map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "immediate", returnType = "java.lang.String")
        public String immediate() {
            return "done";
        }

        @DefineKernelFunction(name = "parallel", returnType = "java.lang.String")
        public Mono<String> parallel(Kernel kernel) {
            return Flux.range(0, 2).flatMap(i -> kernel.<String>invokeAsync("nested", "leaf"))
                .collectList().map(results -> "done");
        }

        @DefineKernelFunction(name = "outer", returnType = "java.lang.String")
        public Mono<String> outer(Kernel kernel) {
            return kernel.<String>invokeAsync("nested", "retried").map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "retried", returnType = "java.lang.String")
        public Mono<String> retried(Kernel kernel) {
            return kernel.<String>invokeAsync("nested", "flaky").retry(1).map(r -> r.getResult());
        }

        @DefineKernelFunction(name = "flaky", returnType = "java.lang.String")
        public Mono<String> flaky() {
            Mono<String> result = Mono.defer(() -> calls.incrementAndGet() == 1
                ? Mono.error(new IllegalStateException("synthetic-retry-error")) : Mono.just("done"));
            return asyncFailure ? Mono.delay(Duration.ofMillis(10)).then(result) : result;
        }

        @DefineKernelFunction(name = "leaf", returnType = "java.lang.String")
        public Mono<String> leaf() {
            return Mono.just("done").delayElement(Duration.ofMillis(10));
        }
    }
}
