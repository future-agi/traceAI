package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.gson.JsonParser;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.contextvariables.ContextVariable;
import com.microsoft.semantickernel.contextvariables.ContextVariableType;
import com.microsoft.semantickernel.contextvariables.ContextVariableTypeConverter.NoopConverter;
import com.microsoft.semantickernel.plugin.KernelPluginFactory;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import com.microsoft.semantickernel.semanticfunctions.annotations.DefineKernelFunction;
import com.microsoft.semantickernel.semanticfunctions.annotations.KernelFunctionParameter;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.context.Context;
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
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** Real annotated functions and exported spans, with no provider or backend credentials. */
class KernelArgumentsTest {
    private static final String QUESTION = "synthetic-user-question";
    private static final Duration TIMEOUT = Duration.ofSeconds(10);
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FITracer tracer;
    private final AtomicInteger started = new AtomicInteger();
    private EchoPlugin echo;
    private Kernel kernel;
    private KernelFunction<String> function;

    @BeforeEach
    void setUp() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .addSpanProcessor(new SpanProcessor() {
                @Override public void onStart(Context parent, ReadWriteSpan span) { started.incrementAndGet(); }
                @Override public boolean isStartRequired() { return true; }
                @Override public void onEnd(ReadableSpan span) { }
                @Override public boolean isEndRequired() { return false; }
            }).build();
        tracer = new FITracer(provider.get("kernel-arguments-test"), TraceConfig.builder().build());
        echo = new EchoPlugin();
        var plugin = KernelPluginFactory.createFromObject(echo, "local");
        kernel = Kernel.builder().withPlugin(plugin).build();
        function = plugin.get("echo");
    }

    @AfterEach
    void tearDown() {
        provider.close();
    }

    @Test
    void exportedInputContainsTheArgumentValueRatherThanItsWrapper() {
        var arguments = arguments();
        assertSameBusinessResult(arguments, tracer);

        assertThat(attribute(SemanticConventions.INPUT_VALUE)).isEqualTo("question: " + QUESTION);
    }

    @Test
    void exportedRawInputIsAFlattenedTypedArgumentMap() {
        var arguments = arguments();
        arguments.put("count", variable(3, Integer.class));
        arguments.put("enabled", variable(true, Boolean.class));
        arguments.put("details", variable(Map.of("topic", "synthetic-topic"), Map.class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.keySet()).containsExactlyInAnyOrder("question", "count", "enabled", "details");
        assertThat(raw.get("question").getAsString()).isEqualTo(QUESTION);
        assertThat(raw.get("count").getAsJsonPrimitive().isNumber()).isTrue();
        assertThat(raw.get("count").getAsInt()).isEqualTo(3);
        assertThat(raw.get("enabled").getAsJsonPrimitive().isBoolean()).isTrue();
        assertThat(raw.get("enabled").getAsBoolean()).isTrue();
        assertThat(raw.getAsJsonObject("details").get("topic").getAsString()).isEqualTo("synthetic-topic");
        // Capture must not replace or mutate the arguments passed to Semantic Kernel.
        assertThat(arguments.get("question").getValue()).isEqualTo(QUESTION);
        assertThat(arguments.get("count").getValue()).isEqualTo(3);
    }

    @Test
    void cyclicMapDoesNotAbortTheFunctionOrLeakItsSpan() {
        var arguments = arguments();
        Map<String, Object> cycle = new LinkedHashMap<>();
        cycle.put("self", cycle);
        arguments.put("unused", variable(cycle, Map.class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.get("question").getAsString()).isEqualTo(QUESTION);
        assertThat(raw.getAsJsonObject("unused").get("self").getAsString()).isEqualTo("[circular]");
        assertThat(cycle.get("self")).isSameAs(cycle);
    }

    @Test
    void indirectMapCycleDoesNotRecurseThroughTheReadableInputFormatter() {
        var arguments = arguments();
        Map<String, Object> first = new LinkedHashMap<>();
        Map<String, Object> second = new LinkedHashMap<>();
        first.put("next", second);
        second.put("next", first);
        arguments.put("unused", variable(first, Map.class));
        assertSameBusinessResult(arguments, tracer);

        assertThat(attribute(SemanticConventions.INPUT_VALUE)).contains("[circular]", "question: " + QUESTION);
        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.getAsJsonObject("unused").getAsJsonObject("next").get("next").getAsString())
            .isEqualTo("[circular]");
    }

    @Test
    void cyclicCollectionsAndArraysAreCapturedWithoutMutation() {
        var arguments = arguments();
        List<Object> list = new ArrayList<>();
        list.add(list);
        Object[] array = new Object[1];
        array[0] = array;
        arguments.put("list", variable(list, List.class));
        arguments.put("array", variable(array, Object[].class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.getAsJsonArray("list").get(0).getAsString()).isEqualTo("[circular]");
        assertThat(raw.getAsJsonArray("array").get(0).getAsString()).isEqualTo("[circular]");
        assertThat(list.get(0)).isSameAs(list);
        assertThat(array[0]).isSameAs(array);
    }

    @Test
    void deeplyNestedAcyclicInputIsBoundedBeforeFormattingOrSerialization() {
        var arguments = arguments();
        Object nested = "leaf";
        for (int i = 0; i < 4096; i++) {
            nested = Map.of("child", List.of(nested));
        }
        arguments.put("unused", variable(nested, Object.class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.get("unused").toString()).contains("[max depth]").doesNotContain("leaf");
        assertThat(raw.get("question").getAsString()).isEqualTo(QUESTION);
    }

    @Test
    void sharedValuesAreNotMistakenForCyclesAndKeepTheirJsonTypes() {
        var arguments = arguments();
        var shared = Map.of("count", 7, "enabled", true, "items", new int[] {1, 2});
        arguments.put("shared", variable(List.of(shared, shared), List.class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        var values = raw.getAsJsonArray("shared");
        assertThat(values.get(0)).isEqualTo(values.get(1));
        assertThat(values.get(0).getAsJsonObject().get("count").getAsJsonPrimitive().isNumber()).isTrue();
        assertThat(values.get(0).getAsJsonObject().get("enabled").getAsJsonPrimitive().isBoolean()).isTrue();
        assertThat(values.get(0).getAsJsonObject().getAsJsonArray("items").get(1).getAsInt()).isEqualTo(2);
    }

    @Test
    void unsupportedCyclicBeansAndMapKeysAreNeverFormattedOrReflected() {
        var arguments = arguments();
        var bean = new CyclicBean();
        arguments.put("bean", variable(bean, CyclicBean.class));
        arguments.put("keys", variable(Map.of(bean, "unused", "healthy", true), Map.class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.get("bean").getAsString()).isEqualTo("[unsupported]");
        assertThat(raw.getAsJsonObject("keys").keySet()).containsExactly("healthy");
        assertThat(raw.getAsJsonObject("keys").get("healthy").getAsBoolean()).isTrue();
        assertThat(bean.self).isSameAs(bean);
    }

    @Test
    void broadSharedGraphsHaveABoundedCaptureBudgetPerArgument() {
        var arguments = arguments();
        Object shared = 1;
        for (int i = 0; i < 20; i++) {
            shared = List.of(shared, shared);
        }
        arguments.put("unused", variable(shared, Object.class));
        assertSameBusinessResult(arguments, tracer);

        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.get("unused").toString()).contains("[truncated]");
        assertThat(raw.get("question").getAsString()).isEqualTo(QUESTION);
        assertThat(attribute(SemanticConventions.RAW_INPUT).length()).isLessThan(10_000);
    }

    @Test
    void throwingWrapperFormatterDoesNotAbortAnOtherwiseSuccessfulInvocation() {
        var arguments = arguments();
        arguments.put("unused", new ContextVariable<String>(stringType(), "synthetic-unused") {
            @Override public String toString() {
                throw new IllegalStateException("synthetic-wrapper-formatter-error");
            }
        });
        assertSameBusinessResult(arguments, tracer);

        assertThat(attribute(SemanticConventions.INPUT_VALUE)).contains("unused: synthetic-unused");
        assertThat(JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT))
            .getAsJsonObject().get("unused").getAsString()).isEqualTo("synthetic-unused");
    }

    @Test
    void throwingValueFormatterDoesNotAbortOrLoseHealthyInputs() {
        var arguments = arguments();
        arguments.put("unused", variable(new ThrowingValue(), ThrowingValue.class));
        assertSameBusinessResult(arguments, tracer);

        assertThat(attribute(SemanticConventions.INPUT_VALUE)).contains("question: " + QUESTION)
            .doesNotContain("ContextVariable@", "synthetic-value-formatter-error");
        var raw = JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT)).getAsJsonObject();
        assertThat(raw.get("question").getAsString()).isEqualTo(QUESTION);
        assertThat(raw.get("unused").getAsString()).isEqualTo("[unsupported]");
    }

    @Test
    void failingRawInputSerializationDoesNotChangeTheFunctionResult() {
        var throwingTracer = new FITracer(provider.get("kernel-arguments-test"), TraceConfig.builder().build()) {
            @Override public void setRawInput(io.opentelemetry.api.trace.Span span, Object value) {
                throw new IllegalStateException("synthetic-serialization-error");
            }
        };
        assertSameBusinessResult(arguments(), throwingTracer);
        assertThat(attribute(SemanticConventions.INPUT_VALUE)).isEqualTo("question: " + QUESTION);
    }

    @Test
    void throwingUnusedValueGetterDoesNotAbortTheInvocation() {
        var arguments = arguments();
        arguments.put("unused", new ContextVariable<String>(stringType(), "synthetic-unused") {
            @Override public String getValue() {
                throw new IllegalStateException("synthetic-getter-error");
            }
        });
        assertSameBusinessResult(arguments, tracer);
    }

    @Test
    void promptInvocationAlsoExportsFlattenedArguments() {
        try (var openAi = new LocalOpenAi()) {
            var promptKernel = Kernel.builder().withAIService(
                com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService.class,
                openAi.chatService()).build();
            var result = new TracedKernel(promptKernel, tracer)
                .invokePromptAsync("Answer {{$question}}", arguments()).block(TIMEOUT);
            assertThat(result.getResult()).isEqualTo(LocalOpenAi.ANSWER);
            var agents = exporter.getFinishedSpanItems().stream()
                .filter(span -> "AGENT".equals(span.getAttributes()
                    .get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND))))
                .toList();
            assertThat(agents).hasSize(1);
            var attributes = agents.get(0).getAttributes();
            assertThat(attributes.get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE)))
                .isEqualTo("question: " + QUESTION);
            assertThat(JsonParser.parseString(attributes.get(AttributeKey.stringKey(SemanticConventions.RAW_INPUT)))
                .getAsJsonObject().get("question").getAsString()).isEqualTo(QUESTION);
        }
    }

    @Test
    void hiddenInputsAreNotInspectedOrExported() {
        var arguments = arguments();
        AtomicInteger reads = new AtomicInteger();
        arguments.put("unused", new ContextVariable<String>(stringType(), "synthetic-hidden") {
            @Override public String getValue() {
                reads.incrementAndGet();
                throw new IllegalStateException("hidden-value-read");
            }
            @Override public String toString() {
                reads.incrementAndGet();
                throw new IllegalStateException("hidden-wrapper-read");
            }
        });
        var hiddenTracer = new FITracer(provider.get("kernel-arguments-test"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).build());
        assertSameBusinessResult(arguments, hiddenTracer);

        assertThat(reads).hasValue(0);
        assertThat(attribute(SemanticConventions.INPUT_VALUE)).isNull();
        assertThat(attribute(SemanticConventions.RAW_INPUT)).isNull();
        assertThat(exporter.getFinishedSpanItems().toString()).doesNotContain(QUESTION, "synthetic-hidden");
    }

    @Test
    void nullArgumentValuesDoNotPreventTheInvocation() {
        var arguments = arguments();
        arguments.put("empty", variable(null, String.class));
        assertSameBusinessResult(arguments, tracer);
        assertThat(attribute(SemanticConventions.INPUT_VALUE)).contains("question: " + QUESTION, "empty: ");
        assertThat(JsonParser.parseString(attribute(SemanticConventions.RAW_INPUT))
            .getAsJsonObject().get("question").getAsString()).isEqualTo(QUESTION);
    }

    private KernelFunctionArguments arguments() {
        return KernelFunctionArguments.builder().withVariable("question", QUESTION).build();
    }

    private void assertSameBusinessResult(KernelFunctionArguments arguments, FITracer tracedWith) {
        var control = kernel.invokeAsync(function).withArguments(arguments).block(TIMEOUT);
        assertThat(control.getResult()).isEqualTo("received: " + QUESTION);
        var traced = new TracedKernel(kernel, tracedWith).invokeAsync(function, arguments).block(TIMEOUT);
        assertThat(traced.getResult()).isEqualTo(control.getResult());
        assertThat(echo.calls).hasValue(2);
        assertThat(agent().getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(agent().getEvents()).isEmpty();
        assertThat(started).hasValue(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(started.get());
        assertThat(agent().hasEnded()).isTrue();
    }

    private SpanData agent() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo("AGENT");
        return span;
    }

    private String attribute(String name) {
        return agent().getAttributes().get(AttributeKey.stringKey(name));
    }

    private static ContextVariableType<String> stringType() {
        return new ContextVariableType<>(new NoopConverter<>(String.class), String.class);
    }

    private static <T> ContextVariable<T> variable(T value, Class<T> type) {
        return new ContextVariable<>(new ContextVariableType<>(new NoopConverter<>(type), type), value);
    }

    public static class EchoPlugin {
        final AtomicInteger calls = new AtomicInteger();

        @DefineKernelFunction(name = "echo", returnType = "java.lang.String")
        public String echo(@KernelFunctionParameter(name = "question") String question) {
            calls.incrementAndGet();
            return "received: " + question;
        }
    }

    public static class CyclicBean {
        final CyclicBean self = this;

        @Override public String toString() {
            throw new AssertionError("instrumentation must not call arbitrary formatters");
        }
    }

    public static class ThrowingValue {
        // Neither Gson's unsupported Class adapter nor this throwing formatter should be invoked.
        final Class<?> unsupported = ThrowingValue.class;

        @Override public String toString() {
            throw new IllegalStateException("synthetic-value-formatter-error");
        }
    }
}
