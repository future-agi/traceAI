package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

import static org.assertj.core.api.Assertions.assertThat;

class BedrockRawOutputTest {
    private static final String PROMPT_SENTINEL = "sentinel-request-prompt-7f3a";

    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @AfterEach
    void reset() { otel.clearSpans(); }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void rawOutputOmitsRequestPromptUnderInputSettings(String flag) {
        // Cohere Command on Bedrock returns the request prompt next to the generations.
        InvokeModelResponse response = InvokeModelResponse.builder().body(SdkBytes.fromUtf8String(
            "{\"id\":\"r1\",\"prompt\":\"" + PROMPT_SENTINEL + "\","
            + "\"generations\":[{\"id\":\"g1\",\"text\":\"model-answer\"}]}")).build();

        // Control: without a flag the raw output is still recorded.
        SpanData control = invoke(response, TraceConfig.builder());
        assertThat(value(control, SemanticConventions.RAW_OUTPUT)).contains("model-answer");

        otel.clearSpans();
        TraceConfig.Builder builder = TraceConfig.builder();
        if (flag.equals("hideInputs")) builder.hideInputs(true); else builder.hideInputMessages(true);
        SpanData span = invoke(response, builder);

        assertThat(span.getAttributes().toString()).doesNotContain(PROMPT_SENTINEL);
        span.getEvents().forEach(e -> assertThat(e.getAttributes().toString()).doesNotContain(PROMPT_SENTINEL));
    }

    private SpanData invoke(InvokeModelResponse response, TraceConfig.Builder builder) {
        TraceConfig config = builder.apiKey("offline").secretKey("offline").projectName("offline").build();
        var client = new TracedBedrockRuntimeClient(BedrockTracingTest.stub(response),
            new FITracer(otel.getOpenTelemetry().getTracer("raw-output-test"), config));
        client.invokeModel(InvokeModelRequest.builder().modelId("cohere.command-text-v14")
            .body(SdkBytes.fromUtf8String("{\"prompt\":\"" + PROMPT_SENTINEL + "\"}")).build());
        return otel.getSpans().get(0);
    }

    private static String value(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }
}
