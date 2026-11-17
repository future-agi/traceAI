package ai.traceai.auto;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.TraceFlags;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class AutoSamplerTest {
    @TempDir Path temporary;

    @Test void unsetRateDoesNotMaskOtelSamplerWithSyntheticDefault() throws Exception {
        AutoConfig config = resolve(Map.of(), Map.of("OTEL_TRACES_SAMPLER", "always_off"));
        assertEquals(1, config.sampleRate, "retain the diagnostic rate default without making it explicit");
        decisions(config, false, false, false);
    }

    @Test void explicitOptionRateWinsOverBothEnvironments() throws Exception {
        decisions(resolve(Map.of("sample_rate", 1), Map.of("FI_SAMPLE_RATE", "0", "OTEL_TRACES_SAMPLER", "always_off")), true, true, false);
    }

    @Test void explicitEnvironmentRateWinsOverOtelSampler() throws Exception {
        decisions(resolve(Map.of(), Map.of("FI_SAMPLE_RATE", "0", "OTEL_TRACES_SAMPLER", "always_on")), false, true, false);
    }

    @Test void configFileRateIsExplicitAndKeepsExistingPrecedence() throws Exception {
        Path file = temporary.resolve("sampler.toml");
        Files.writeString(file, "sample_rate = 0\n");
        Map<String, Object> options = Map.of("config_file", file.toString());
        decisions(resolve(options, Map.of("OTEL_TRACES_SAMPLER", "always_on")), false, true, false);
        decisions(resolve(options, Map.of("FI_SAMPLE_RATE", "1", "OTEL_TRACES_SAMPLER", "always_off")), true, true, false);
    }

    @Test void invalidExplicitRateKeepsExistingRateOneFallback() throws Exception {
        decisions(resolve(Map.of("sample_rate", "NaN"), Map.of("OTEL_TRACES_SAMPLER", "always_off")), true, true, false);
    }

    private AutoConfig resolve(Map<String, ?> overrides, Map<String, String> environment) {
        Map<String, Object> options = new LinkedHashMap<>();
        options.put("config_file", temporary.resolve("absent.toml").toString());
        options.putAll(overrides);
        return AutoConfig.resolve(options, environment, temporary);
    }

    private static void decisions(AutoConfig config, boolean root, boolean sampledChild, boolean unsampledChild) throws Exception {
        // Reflection allows a named RED failure on the old config API rather than a compilation failure.
        Sampler sampler = (Sampler) AutoConfig.class.getDeclaredMethod("sampler").invoke(config);
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(sampler).build()) {
            Span span = provider.get("sampler-test").spanBuilder("root").setNoParent().startSpan();
            assertEquals(root, span.isRecording()); assertEquals(root, span.getSpanContext().isSampled()); span.end();
            for (boolean sampled : new boolean[]{true, false}) {
                SpanContext parent = SpanContext.createFromRemoteParent("12345678901234567890123456789012", "1234567890123456",
                    sampled ? TraceFlags.getSampled() : TraceFlags.getDefault(), TraceState.getDefault());
                Span child = provider.get("sampler-test").spanBuilder("child").setParent(Context.root().with(Span.wrap(parent))).startSpan();
                boolean expected = sampled ? sampledChild : unsampledChild;
                assertEquals(expected, child.isRecording()); assertEquals(expected, child.getSpanContext().isSampled());
                assertEquals(parent.getTraceId(), child.getSpanContext().getTraceId());
                assertEquals(sampled, parent.isSampled()); child.end();
            }
        }
    }
}
