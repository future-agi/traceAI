package ai.traceai.azure.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.models.ChatCompletions;
import com.azure.ai.openai.models.ChatCompletionsOptions;
import com.azure.ai.openai.models.ChatRequestUserMessage;
import com.azure.core.util.BinaryData;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/** Output tool-call arguments are content: the hide settings must mask them. */
class ToolCallArgumentsMaskTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String RESPONSE = "{\"id\":\"resp-1\",\"model\":\"gpt-4o\",\"created\":1,"
        + "\"choices\":[{\"index\":0,\"finish_reason\":\"tool_calls\",\"message\":{\"role\":\"assistant\","
        + "\"content\":null,\"tool_calls\":[{\"id\":\"call-1\",\"type\":\"function\",\"function\":"
        + "{\"name\":\"lookup\",\"arguments\":\"{\\\"ssn\\\":\\\"private-tool-arguments-DO-NOT-EXPORT\\\"}\"}}]}}]}";

    private SpanData export(TraceConfig config) {
        otel.clearSpans();
        OpenAIClient client = mock(OpenAIClient.class);
        when(client.getChatCompletions(anyString(), any(ChatCompletionsOptions.class)))
            .thenReturn(BinaryData.fromString(RESPONSE).toObject(ChatCompletions.class));
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
        new TracedAzureOpenAIClient(client, tracer).getChatCompletions("gpt-4o",
            new ChatCompletionsOptions(List.of(new ChatRequestUserMessage("hello"))));
        return otel.getSpans().get(0);
    }

    private static boolean exportsSecret(SpanData data) {
        return data.getAttributes().asMap().values().stream().anyMatch(v -> String.valueOf(v).contains("private-tool-arguments"));
    }

    @Test
    void argumentsAreExportedWhenNothingIsHidden() {
        assertThat(exportsSecret(export(TraceConfig.builder().build()))).isTrue();
    }

    @Test
    void hideOutputsMasksToolCallArguments() {
        assertThat(exportsSecret(export(TraceConfig.builder().hideOutputs(true).build()))).isFalse();
    }

    @Test
    void hideOutputMessagesMasksToolCallArguments() {
        assertThat(exportsSecret(export(TraceConfig.builder().hideOutputMessages(true).build()))).isFalse();
    }

    @Test
    void hideInputsMasksToolCallArguments() {
        // Python lists tool_call.function.arguments on the input side.
        assertThat(exportsSecret(export(TraceConfig.builder().hideInputs(true).build()))).isFalse();
    }

    @Test
    void toolNameStaysVisibleUnderHideInputs() {
        SpanData data = export(TraceConfig.builder().hideInputs(true).build());
        assertThat(data.getAttributes().asMap().values().stream().map(String::valueOf)).contains("lookup");
    }
}
