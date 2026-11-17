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

    @Test void ownedAgentSamplerStampsExplicitAndStandardRates() throws Exception {
        for (AutoConfig config : java.util.List.of(
                resolve(Map.of("sample_rate", 0.25), Map.of("OTEL_TRACES_SAMPLER", "always_off")),
                resolve(Map.of(), Map.of("FI_SAMPLE_RATE", "0.25")),
                resolve(Map.of(), Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0.25")))) {
            Sampler sampler = ownedSampler(config);
            var root = sample(sampler, Context.root());
            assertEquals(io.opentelemetry.sdk.trace.samplers.SamplingDecision.RECORD_AND_SAMPLE, root.getDecision());
            assertEquals(Double.valueOf(0.25), root.getAttributes().get(io.opentelemetry.api.common.AttributeKey.doubleKey("fi.sampling.ratio")));
            assertEquals("r:0.25", root.getUpdatedTraceState(TraceState.getDefault()).get("fi"));
            for (boolean remote : new boolean[]{true, false}) {
                TraceState state = TraceState.builder().put("fi", "r:0.5").put("vendor", "preserved").build();
                SpanContext parent = remote
                    ? SpanContext.createFromRemoteParent("00000000000000000000000000000001", "0000000000000001", TraceFlags.getSampled(), state)
                    : SpanContext.create("00000000000000000000000000000001", "0000000000000001", TraceFlags.getSampled(), state);
                var child = sample(sampler, Context.root().with(Span.wrap(parent)));
                assertEquals(Double.valueOf(0.5), child.getAttributes().get(io.opentelemetry.api.common.AttributeKey.doubleKey("fi.sampling.ratio")));
                assertEquals(state, child.getUpdatedTraceState(state));
            }
        }
    }

    @Test void ownedAgentNonParentBasedUsesOwnRatioButDefaultDoesNotStamp() throws Exception {
        TraceState incoming = TraceState.builder().put("fi", "r:0.5").put("vendor", "preserved").build();
        SpanContext parent = SpanContext.createFromRemoteParent("00000000000000000000000000000001", "0000000000000001", TraceFlags.getSampled(), incoming);
        Sampler sampler = ownedSampler(resolve(Map.of(), Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0.25")));
        var result = sample(sampler, Context.root().with(Span.wrap(parent)));
        assertEquals(Double.valueOf(0.25), result.getAttributes().get(io.opentelemetry.api.common.AttributeKey.doubleKey("fi.sampling.ratio")));
        assertEquals("r:0.25", result.getUpdatedTraceState(incoming).get("fi"));
        assertEquals("preserved", result.getUpdatedTraceState(incoming).get("vendor"));
        var defaults = sample(ownedSampler(resolve(Map.of(), Map.of())), Context.root());
        assertNull(defaults.getAttributes().get(io.opentelemetry.api.common.AttributeKey.doubleKey("fi.sampling.ratio")));
        assertNull(defaults.getUpdatedTraceState(TraceState.getDefault()).get("fi"));
    }

    private static Sampler ownedSampler(AutoConfig config) throws Exception {
        return (Sampler) TraceAIAuto.class.getDeclaredMethod("ownedSampler", AutoConfig.class).invoke(null, config);
    }

    private static io.opentelemetry.sdk.trace.samplers.SamplingResult sample(Sampler sampler, Context parent) {
        return sampler.shouldSample(parent, "00000000000000000000000000000001", "test",
            io.opentelemetry.api.trace.SpanKind.INTERNAL, io.opentelemetry.api.common.Attributes.empty(), java.util.List.of());
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
