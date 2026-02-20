package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.orchestration.FunctionInvocation;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import reactor.core.publisher.Mono;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedKernelTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private Kernel kernel;

    @Mock
    private KernelFunction<String> function;

    @Mock
    private FunctionInvocation<String> functionInvocation;

    private FITracer tracer;
    private TracedKernel tracedKernel;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
        tracedKernel = new TracedKernel(kernel, tracer);
    }

    @Test
    void shouldSetCorrectSpanKindForKernelInvocation() {
        // Given
        when(function.getName()).thenReturn("testFunction");
        when(function.getPluginName()).thenReturn("testPlugin");

        FunctionResult<String> mockResult = mock(FunctionResult.class);
        when(mockResult.getResult()).thenReturn("test result");

        when(kernel.invokeAsync(any(KernelFunction.class))).thenReturn(functionInvocation);
        when(functionInvocation.withArguments(any())).thenReturn(functionInvocation);
        when(functionInvocation.doOnSuccess(any())).thenAnswer(invocation -> {
            java.util.function.Consumer<FunctionResult<String>> consumer = invocation.getArgument(0);
            consumer.accept(mockResult);
            return functionInvocation;
        });
        when(functionInvocation.doOnError(any())).thenReturn(functionInvocation);
        when(functionInvocation.doFinally(any())).thenAnswer(invocation -> {
            java.util.function.Consumer<reactor.core.publisher.SignalType> consumer = invocation.getArgument(0);
            consumer.accept(reactor.core.publisher.SignalType.ON_COMPLETE);
            return Mono.just(mockResult);
        });

        // When
        tracedKernel.invokeAsync(function, KernelFunctionArguments.builder().build()).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData spanData = spans.get(0);
        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)
        )).isEqualTo(FISpanKind.AGENT.getValue());
    }

    @Test
    void shouldSetSemanticKernelAttributes() {
        // Given
        String functionName = "summarize";
        String pluginName = "TextPlugin";

        when(function.getName()).thenReturn(functionName);
        when(function.getPluginName()).thenReturn(pluginName);

        FunctionResult<String> mockResult = mock(FunctionResult.class);
        when(mockResult.getResult()).thenReturn("summary");

        when(kernel.invokeAsync(any(KernelFunction.class))).thenReturn(functionInvocation);
        when(functionInvocation.withArguments(any())).thenReturn(functionInvocation);
        when(functionInvocation.doOnSuccess(any())).thenAnswer(invocation -> {
            java.util.function.Consumer<FunctionResult<String>> consumer = invocation.getArgument(0);
            consumer.accept(mockResult);
            return functionInvocation;
        });
        when(functionInvocation.doOnError(any())).thenReturn(functionInvocation);
        when(functionInvocation.doFinally(any())).thenAnswer(invocation -> {
            java.util.function.Consumer<reactor.core.publisher.SignalType> consumer = invocation.getArgument(0);
            consumer.accept(reactor.core.publisher.SignalType.ON_COMPLETE);
            return Mono.just(mockResult);
        });

        // When
        tracedKernel.invokeAsync(function, KernelFunctionArguments.builder().build()).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        SpanData spanData = spans.get(0);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)
        )).isEqualTo("semantic-kernel");

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("semantic_kernel.function_name")
        )).isEqualTo(functionName);

        assertThat(spanData.getAttributes().get(
            AttributeKey.stringKey("semantic_kernel.plugin_name")
        )).isEqualTo(pluginName);
    }

    @Test
    void shouldCaptureSpanNameWithPluginAndFunction() {
        // Given
        when(function.getName()).thenReturn("translate");
        when(function.getPluginName()).thenReturn("LanguagePlugin");

        FunctionResult<String> mockResult = mock(FunctionResult.class);
        when(mockResult.getResult()).thenReturn("translated");

        when(kernel.invokeAsync(any(KernelFunction.class))).thenReturn(functionInvocation);
        when(functionInvocation.withArguments(any())).thenReturn(functionInvocation);
        when(functionInvocation.doOnSuccess(any())).thenAnswer(invocation -> {
            java.util.function.Consumer<FunctionResult<String>> consumer = invocation.getArgument(0);
            consumer.accept(mockResult);
            return functionInvocation;
        });
        when(functionInvocation.doOnError(any())).thenReturn(functionInvocation);
        when(functionInvocation.doFinally(any())).thenAnswer(invocation -> {
            java.util.function.Consumer<reactor.core.publisher.SignalType> consumer = invocation.getArgument(0);
            consumer.accept(reactor.core.publisher.SignalType.ON_COMPLETE);
            return Mono.just(mockResult);
        });

        // When
        tracedKernel.invokeAsync(function, KernelFunctionArguments.builder().build()).block();

        // Then
        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans.get(0).getName()).isEqualTo("Semantic Kernel: LanguagePlugin.translate");
    }

    @Test
    void shouldUnwrapToOriginalKernel() {
        assertThat(tracedKernel.unwrap()).isSameAs(kernel);
    }

    @Test
    void shouldReturnTracer() {
        assertThat(tracedKernel.getTracer()).isSameAs(tracer);
    }
}
