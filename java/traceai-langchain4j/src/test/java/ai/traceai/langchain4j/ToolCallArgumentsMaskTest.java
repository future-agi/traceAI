package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/** Tool-call arguments returned by the model are content: the hide settings must mask them. */
class ToolCallArgumentsMaskTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String SECRET_ARGS = "{\"ssn\":\"private-tool-arguments-DO-NOT-EXPORT\"}";
    private static final String ARGS_KEY = "message.tool_calls.0.tool_call.function.arguments";

    @SuppressWarnings("unchecked")
    private SpanData export(TraceConfig config) {
        otel.clearSpans();
        ChatLanguageModel delegate = mock(ChatLanguageModel.class);
        ToolExecutionRequest request = ToolExecutionRequest.builder()
            .id("call-1").name("lookup").arguments(SECRET_ARGS).build();
        when(delegate.generate(anyList())).thenReturn(Response.from(AiMessage.from("ok", List.of(request))));
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
        new TracedChatLanguageModel(delegate, tracer, "openai")
            .generate(List.<ChatMessage>of(UserMessage.from("hello")));
        return otel.getSpans().get(0);
    }

    private static boolean exportsSecret(SpanData data) {
        return data.getAttributes().asMap().values().stream().anyMatch(v -> String.valueOf(v).contains("private-tool-arguments"));
    }

    private static String arguments(SpanData data) {
        return data.getAttributes().asMap().entrySet().stream()
            .filter(e -> e.getKey().getKey().equals(ARGS_KEY)).map(e -> String.valueOf(e.getValue())).findFirst().orElse(null);
    }

    @Test
    void argumentsAreExportedWhenNothingIsHidden() {
        assertThat(arguments(export(TraceConfig.builder().build()))).isEqualTo(SECRET_ARGS);
    }

    @Test
    void hideOutputsMasksToolCallArguments() {
        SpanData data = export(TraceConfig.builder().hideOutputs(true).build());
        assertThat(exportsSecret(data)).isFalse();
        assertThat(arguments(data)).isIn(null, "__REDACTED__");
    }

    @Test
    void hideInputsMasksToolCallArguments() {
        // Python lists tool_call.function.arguments on the input side.
        assertThat(exportsSecret(export(TraceConfig.builder().hideInputs(true).build()))).isFalse();
    }

    @Test
    void hideOutputMessagesMasksToolCallArguments() {
        assertThat(exportsSecret(export(TraceConfig.builder().hideOutputMessages(true).build()))).isFalse();
    }

    @Test
    void toolNameAndIdStayVisibleUnderHideInputs() {
        SpanData data = export(TraceConfig.builder().hideInputs(true).build());
        assertThat(data.getAttributes().asMap().values().stream().map(String::valueOf))
            .contains("lookup", "call-1");
    }
}
