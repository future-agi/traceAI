package ai.traceai.vertexai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.cloud.vertexai.api.Content;
import com.google.cloud.vertexai.api.Part;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

class VertexAIEnvironmentPrivacyTest {
    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void environmentFlagsHideRealSdkUnaryAndStreamingContent(boolean messagesOnly) throws Exception {
        ProcessBuilder builder = new ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(),
            "-cp", System.getProperty("java.class.path"), Probe.class.getName(), Boolean.toString(messagesOnly));
        builder.environment().put("FI_HIDE_INPUTS", Boolean.toString(!messagesOnly));
        builder.environment().put("FI_HIDE_OUTPUTS", Boolean.toString(!messagesOnly));
        builder.environment().put("FI_HIDE_INPUT_MESSAGES", "true");
        builder.environment().put("FI_HIDE_OUTPUT_MESSAGES", "true");
        builder.environment().put("FI_HIDE_ERROR_DETAILS", "true");
        Process process = builder.redirectErrorStream(true).start();
        try {
            assertThat(process.waitFor(45, TimeUnit.SECONDS)).isTrue();
            assertThat(process.exitValue()).withFailMessage(new String(process.getInputStream().readAllBytes())).isZero();
        } finally {
            process.destroyForcibly();
        }
    }

    public static class Probe {
        public static void main(String[] args) throws Exception {
            boolean messagesOnly = Boolean.parseBoolean(args[0]);
            var exporter = InMemorySpanExporter.create();
            try (var provider = SdkTracerProvider.builder()
                    .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
                 var service = new FakeVertexService()) {
                service.unaryResponse = VertexAIStreamTracingTest.lastChunk("synthetic-private-answer");
                service.streamChunks = List.of(service.unaryResponse);
                var inputs = List.of(Content.newBuilder().setRole("user")
                    .addParts(Part.newBuilder().setText("synthetic-private-prompt")).build());
                for (var config : List.of(TraceConfig.builder().build(), TraceConfig.fromEnvironment())) {
                    var model = new TracedGenerativeModel(new GenerativeModel("gemini-test", service.vertexAI),
                        new FITracer(provider.get("environment-test"), config));
                    assertThat(model.generateContent(inputs)).isEqualTo(service.unaryResponse);
                    assertThat(model.generateContentStream("synthetic-private-prompt").stream().toList()).containsExactly(service.unaryResponse);
                }
                assertThat(exporter.getFinishedSpanItems()).hasSize(4).allSatisfy(span -> {
                    var keys = span.getAttributes().asMap().keySet().stream().map(key -> key.getKey()).toList();
                    assertThat(keys).doesNotContain("gen_ai.input.messages", "gen_ai.output.messages");
                    if (messagesOnly) {
                        assertThat(keys).contains("output.value");
                        assertThat(keys).contains(span.getName().endsWith("(Stream)") ? "input.value" : "raw.input");
                    } else {
                        assertThat(keys).doesNotContain("input.value", "output.value", "raw.input", "raw.output",
                            "fi.raw_input", "fi.raw_output");
                        assertThat(span.getAttributes().toString()).doesNotContain("synthetic-private");
                    }
                });
            }
        }
    }
}
