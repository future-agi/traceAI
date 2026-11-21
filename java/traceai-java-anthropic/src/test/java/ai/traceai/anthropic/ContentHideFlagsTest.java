package ai.traceai.anthropic;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.anthropic.core.ObjectMappers;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Prompt and response content must not be exported under the hide flags. Offline: the SDK client is a stub
 * returning a response parsed by the real SDK mapper; no transport, no credentials.
 */
class ContentHideFlagsTest {
    private static final String IN = "SENTINEL-INPUT-PROMPT-DO-NOT-EXPORT";
    private static final String OUT = "SENTINEL-OUTPUT-RESPONSE-DO-NOT-EXPORT";
    private static final String MESSAGE = "{\"id\":\"msg_1\",\"type\":\"message\",\"role\":\"assistant\","
        + "\"model\":\"test-model\",\"content\":[{\"type\":\"text\",\"text\":\"" + OUT + "\"}],"
        + "\"stop_reason\":\"end_turn\",\"stop_sequence\":null,"
        + "\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}";

    public static class FakeClient {
        final FakeMessages api;
        FakeClient(Object response) { api = new FakeMessages(response); }
        public FakeMessages messages() { return api; }
    }

    public static class FakeMessages {
        private final Object response;
        FakeMessages(Object response) { this.response = response; }
        public Object create(Object params) { return response; }
    }

    private static List<SpanData> run(TraceConfig config) throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            Object params = SdkTestSupport.call(SdkTestSupport.paramsBuilder(IN), "build");
            Object response = ObjectMappers.jsonMapper().readValue(MESSAGE, SdkTestSupport.modelClass("Message"));
            new TracedAnthropicClient(new FakeClient(response), new FITracer(provider.get("t"), config))
                .createMessage(params);
            return exporter.getFinishedSpanItems();
        } finally {
            provider.close();
        }
    }

    private static List<String> leakedKeys(List<SpanData> spans, String secret) {
        List<String> leaked = new ArrayList<>();
        for (SpanData span : spans) {
            span.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(secret)) leaked.add(k.getKey());
            });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(secret)) leaked.add("event:" + k.getKey());
            }));
        }
        return leaked;
    }

    @Test
    void controlExportsContentWhenNothingIsHidden() throws Exception {
        List<SpanData> spans = run(TraceConfig.builder().build());
        assertThat(leakedKeys(spans, IN)).contains("fi.raw_input");
        assertThat(leakedKeys(spans, OUT)).contains("output.value", "fi.raw_output");
    }

    @ParameterizedTest(name = "{0}")
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void hiddenInputsAreNotExported(String flag) throws Exception {
        TraceConfig config = flag.equals("hideInputs")
            ? TraceConfig.builder().hideInputs(true).build()
            : TraceConfig.builder().hideInputMessages(true).build();
        List<SpanData> spans = run(config);
        assertThat(spans).isNotEmpty();
        assertThat(leakedKeys(spans, IN)).as("keys exported with the prompt").isEmpty();
    }

    @ParameterizedTest(name = "{0}")
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void hiddenOutputsAreNotExported(String flag) throws Exception {
        TraceConfig config = flag.equals("hideOutputs")
            ? TraceConfig.builder().hideOutputs(true).build()
            : TraceConfig.builder().hideOutputMessages(true).build();
        List<SpanData> spans = run(config);
        assertThat(spans).isNotEmpty();
        assertThat(leakedKeys(spans, OUT)).as("keys exported with the response").isEmpty();
    }
}
