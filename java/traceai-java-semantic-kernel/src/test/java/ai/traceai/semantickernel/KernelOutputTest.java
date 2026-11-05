package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.gson.JsonParser;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.plugin.KernelPluginFactory;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import reactor.core.scheduler.Schedulers;

import static org.assertj.core.api.Assertions.assertThat;

/** Successful real SDK invocations must not depend on whether their results can be serialized. */
class KernelOutputTest {
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private final AtomicInteger started = new AtomicInteger();
    private final AtomicInteger ended = new AtomicInteger();
    private ResultPlugin delegate;
    private Kernel kernel;
    private KernelFunction<Map<String, Object>> function;

    @BeforeEach
    void setUp() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .addSpanProcessor(new SpanProcessor() {
                @Override public void onStart(Context parent, ReadWriteSpan span) { started.incrementAndGet(); }
                @Override public boolean isStartRequired() { return true; }
                @Override public void onEnd(ReadableSpan span) { ended.incrementAndGet(); }
                @Override public boolean isEndRequired() { return true; }
            }).build();
        delegate = new ResultPlugin();
        var plugin = KernelPluginFactory.createFromObject(delegate, "local");
        kernel = Kernel.builder().withPlugin(plugin).build();
        function = plugin.get("result");
    }

    @AfterEach
    void tearDown() {
        provider.close();
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void indirectMapCycleCompletesWithOriginalValueAndEndsItsSpan(boolean hideOutputs) throws Exception {
        Map<String, Object> first = new LinkedHashMap<>();
        Map<String, Object> second = new LinkedHashMap<>();
        first.put("private", "synthetic-output-secret");
        first.put("next", second);
        second.put("next", first);
        assertSuccessfulResult(first, tracer(hideOutputs));

        assertThat(first.get("next")).isSameAs(second);
        assertThat(second.get("next")).isSameAs(first);
        if (hideOutputs) {
            assertHiddenOutput();
        } else {
            assertThat(attribute(SemanticConventions.OUTPUT_VALUE)).contains("[circular]");
            var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_OUTPUT)).getAsJsonObject();
            assertThat(raw.getAsJsonObject("next").get("next").getAsString()).isEqualTo("[circular]");
            assertThat(raw.get("private").getAsString()).isEqualTo("synthetic-output-secret");
        }
    }

    @Test
    void selfReferentialMapDoesNotReachGsonAsTheOriginalObjectGraph() throws Exception {
        Map<String, Object> cycle = new LinkedHashMap<>();
        cycle.put("self", cycle);
        assertSuccessfulResult(cycle, tracer(false));
        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_OUTPUT)).getAsJsonObject();
        assertThat(raw.get("self").getAsString()).isEqualTo("[circular]");
        assertThat(cycle.get("self")).isSameAs(cycle);
    }

    @Test
    void cyclicListsAndArraysAreCopiedWithoutMutation() throws Exception {
        List<Object> list = new ArrayList<>();
        Object[] array = new Object[1];
        list.add(array);
        array[0] = list;
        assertSuccessfulResult(list, tracer(false));
        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_OUTPUT))
            .getAsJsonObject().getAsJsonArray("value");
        assertThat(raw.get(0).getAsJsonArray().get(0).getAsString()).isEqualTo("[circular]");
        assertThat(list.get(0)).isSameAs(array);
        assertThat(array[0]).isSameAs(list);
    }

    @Test
    void deeplyNestedResultsAreBoundedBeforeFormatting() throws Exception {
        Object nested = "leaf";
        for (int i = 0; i < 4096; i++) {
            nested = Map.of("child", List.of(nested));
        }
        assertSuccessfulResult(nested, tracer(false));
        assertThat(attribute(SemanticConventions.RAW_OUTPUT)).contains("[max depth]").doesNotContain("leaf");
    }

    @Test
    void broadSharedGraphsHaveABoundedCaptureBudget() throws Exception {
        Object shared = 1;
        for (int i = 0; i < 20; i++) {
            shared = List.of(shared, shared);
        }
        assertSuccessfulResult(shared, tracer(false));
        assertThat(attribute(SemanticConventions.RAW_OUTPUT)).contains("[truncated]").hasSizeLessThan(10_000);
    }

    @Test
    void rawOutputContainsTypedValuesWithoutSdkWrappers() throws Exception {
        var shared = Map.of("count", 7, "enabled", true, "items", new int[] {1, 2});
        assertSuccessfulResult(List.of(shared, shared), tracer(false));
        var values = JsonParser.parseString(attribute(SemanticConventions.RAW_OUTPUT))
            .getAsJsonObject().getAsJsonArray("value");
        assertThat(values.get(0)).isEqualTo(values.get(1));
        assertThat(values.get(0).getAsJsonObject().get("count").getAsJsonPrimitive().isNumber()).isTrue();
        assertThat(values.get(0).getAsJsonObject().get("enabled").getAsJsonPrimitive().isBoolean()).isTrue();
        assertThat(values.get(0).getAsJsonObject().getAsJsonArray("items").get(1).getAsInt()).isEqualTo(2);
    }

    @Test
    void nestedScalarOutputRetainsItsReadableValue() throws Exception {
        assertSuccessfulResult("synthetic-answer", tracer(false));
        assertThat(attribute(SemanticConventions.OUTPUT_VALUE)).isEqualTo("{value=synthetic-answer}");
        assertThat(JsonParser.parseString(attribute(SemanticConventions.RAW_OUTPUT))
            .getAsJsonObject().get("value").getAsString()).isEqualTo("synthetic-answer");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void arbitraryFormattersAndCyclicBeansAreNotInvokedOrReflected(boolean hideOutputs) throws Exception {
        var bean = new HostileBean();
        assertSuccessfulResult(bean, tracer(hideOutputs));
        assertThat(bean.formats).hasValue(0);
        assertThat(bean.self).isSameAs(bean);
        if (hideOutputs) {
            assertHiddenOutput();
        } else {
            assertThat(attribute(SemanticConventions.OUTPUT_VALUE)).isEqualTo("{value=[unsupported]}");
            assertThat(JsonParser.parseString(attribute(SemanticConventions.RAW_OUTPUT))
                .getAsJsonObject().get("value").getAsString()).isEqualTo("[unsupported]");
        }
    }

    @Test
    void hiddenOutputContainersAreNotInspected() throws Exception {
        var hidden = new UnreadableMap();
        assertSuccessfulResult(hidden, tracer(true));
        assertThat(hidden.reads).hasValue(0);
        assertHiddenOutput();
    }

    @Test
    void containerAccessFailuresDoNotChangeTheSuccessfulResult() throws Exception {
        var unreadable = new UnreadableMap();
        assertSuccessfulResult(unreadable, tracer(false));
        assertThat(attribute(SemanticConventions.OUTPUT_VALUE)).isEqualTo("[unavailable]");
    }

    @Test
    void serializationFailuresDoNotChangeTheSuccessfulResult() throws Exception {
        var failingTracer = new FITracer(provider.get("kernel-output-test"), TraceConfig.builder().build()) {
            @Override public void setRawOutput(io.opentelemetry.api.trace.Span span, Object value) {
                throw new IllegalStateException("synthetic-serialization-error");
            }
        };
        assertSuccessfulResult("synthetic-answer", failingTracer);
        assertThat(attribute(SemanticConventions.OUTPUT_VALUE)).isEqualTo("{value=synthetic-answer}");
    }

    private FITracer tracer(boolean hideOutputs) {
        return new FITracer(provider.get("kernel-output-test"), TraceConfig.builder().hideOutputs(hideOutputs).build());
    }

    @SuppressWarnings("unchecked")
    private void assertSuccessfulResult(Object value, FITracer tracer) throws Exception {
        // The SDK requires a concrete supported return type on annotated methods, not Object.
        Map<String, Object> output = value instanceof Map<?, ?> ? (Map<String, Object>) value : Map.of("value", value);
        delegate.value = output;
        var control = kernel.invokeAsync(function).block(Duration.ofSeconds(5));
        assertThat(control.getResult()).isSameAs(output);

        var received = new AtomicReference<FunctionResult<Map<String, Object>>>();
        var error = new AtomicReference<Throwable>();
        var completed = new CountDownLatch(1);
        var subscription = new TracedKernel(kernel, tracer).invokeAsync(function)
            .subscribeOn(Schedulers.boundedElastic())
            .subscribe(received::set, failure -> { error.set(failure); completed.countDown(); }, completed::countDown);
        try {
            // Observe completion and span closure before cancellation, which could conceal a leaked span.
            boolean done = completed.await(5, TimeUnit.SECONDS);
            assertThat(delegate.calls).hasValue(2);
            assertThat(started).hasValue(1);
            assertThat(ended).as("ended spans before explicit cancellation").hasValue(1);
            assertThat(done).as("successful invocation completed").isTrue();
            assertThat(error.get()).isNull();
            assertThat(received.get()).isNotNull();
            assertThat(received.get().getResult()).isSameAs(output);
            assertThat(agent().getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(agent().getEvents()).isEmpty();
        } finally {
            subscription.dispose();
        }
    }

    private void assertHiddenOutput() {
        assertThat(attribute(SemanticConventions.OUTPUT_VALUE)).isNull();
        assertThat(attribute(SemanticConventions.RAW_OUTPUT)).isNull();
        assertThat(exporter.getFinishedSpanItems().toString()).doesNotContain("synthetic-output-secret");
    }

    private SpanData agent() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo("AGENT");
        assertThat(span.hasEnded()).isTrue();
        return span;
    }

    private String attribute(String name) {
        return agent().getAttributes().get(AttributeKey.stringKey(name));
    }

    public static class ResultPlugin {
        final AtomicInteger calls = new AtomicInteger();
        Map<String, Object> value;

        @DefineKernelFunction(name = "result", returnType = "java.util.Map")
        public Map<String, Object> result() {
            calls.incrementAndGet();
            return value;
        }
    }

    public static class HostileBean {
        final HostileBean self = this;
        final AtomicInteger formats = new AtomicInteger();

        @Override public String toString() {
            formats.incrementAndGet();
            throw new AssertionError("instrumentation must not call arbitrary formatters");
        }
    }

    public static class UnreadableMap extends LinkedHashMap<String, Object> {
        final AtomicInteger reads = new AtomicInteger();

        @Override public Set<Map.Entry<String, Object>> entrySet() {
            reads.incrementAndGet();
            throw new IllegalStateException("synthetic-output-secret");
        }

        @Override public String toString() {
            reads.incrementAndGet();
            throw new IllegalStateException("synthetic-output-secret");
        }
    }
}
