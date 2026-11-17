package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.TraceFlags;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.IdGenerator;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.LinkData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import io.opentelemetry.sdk.trace.samplers.SamplingDecision;
import io.opentelemetry.sdk.trace.samplers.SamplingResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/** All sampling/export checks stay in memory; no collector or credentials are needed. */
class SamplingRatioTest {
    private static final AttributeKey<Double> RATIO = AttributeKey.doubleKey("fi.sampling.ratio");
    private static final String KEPT = "00000000000000000000000000000001";
    private static final String DROPPED = "00000000000000007fffffffffffffff";
    private static final String SPAN_ID = "0000000000000001";

    // Additive APIs are looked up so the tests-first commit compiles on the baseline.
    private static Sampler owned(Sampler sampler) throws Exception {
        return (Sampler) TraceConfig.class.getMethod("samplerForOwnedProvider", Sampler.class).invoke(null, sampler);
    }

    private static Sampler ratio(double rate, boolean parentBased) throws Exception {
        return owned(TraceConfig.samplerFromEnvironment(Map.of(
            "OTEL_TRACES_SAMPLER", parentBased ? "parentbased_traceidratio" : "traceidratio",
            "OTEL_TRACES_SAMPLER_ARG", Double.toString(rate))));
    }

    private static SamplingResult sample(Sampler sampler, Context parent, String traceId) {
        return sampler.shouldSample(parent, traceId, "test", SpanKind.INTERNAL, Attributes.empty(), List.of());
    }

    private static Context parent(boolean remote, boolean sampled, TraceState state) {
        return Context.root().with(Span.wrap(remote
            ? SpanContext.createFromRemoteParent(KEPT, SPAN_ID, sampled ? TraceFlags.getSampled() : TraceFlags.getDefault(), state)
            : SpanContext.create(KEPT, SPAN_ID, sampled ? TraceFlags.getSampled() : TraceFlags.getDefault(), state)));
    }

    private static TraceState state(String ratio) {
        var builder = TraceState.builder().put("vendor", "preserved");
        if (ratio != null) builder.put("fi", ratio);
        return builder.build();
    }

    @ParameterizedTest @ValueSource(doubles = {0.25, 0.12345678901234568, 1.0e-10})
    void keptRootAndLocalChildExportTheSameRatio(double rate) throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(ratio(rate, true))
                .setIdGenerator(ids(KEPT)).addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            Span root = provider.get("test").spanBuilder("root").setNoParent().startSpan();
            assertTrue(root.getSpanContext().isSampled());
            assertEquals("r:" + Double.toString(rate), root.getSpanContext().getTraceState().get("fi"));
            Span child = provider.get("test").spanBuilder("child").setParent(Context.root().with(root)).startSpan();
            assertEquals(root.getSpanContext().getTraceState(), child.getSpanContext().getTraceState());
            child.end(); root.end();
            assertEquals(2, exporter.getFinishedSpanItems().size());
            exporter.getFinishedSpanItems().forEach(span -> assertEquals(Double.valueOf(rate), span.getAttributes().get(RATIO)));
        }
    }

    @Test void parentBasedInheritsLocalAndRemoteRatiosRatherThanItsOwn() throws Exception {
        for (boolean remote : new boolean[]{false, true}) {
            TraceState incoming = state("r:0.5");
            SamplingResult result = sample(ratio(0.25, true), parent(remote, true, incoming), KEPT);
            assertEquals(SamplingDecision.RECORD_AND_SAMPLE, result.getDecision());
            assertEquals(Double.valueOf(0.5), result.getAttributes().get(RATIO));
            assertEquals(incoming, result.getUpdatedTraceState(incoming));
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"r:2", "r:abc", "r:0", "r:1", "r:-0.1", "r:NaN", "r:Infinity", "r:-Infinity", "other:0.5", "r:", "r: 0.5", "r:0x1.0p-1", "r:0.5d"})
    void malformedParentRatioIsNotStamped(String value) throws Exception {
        for (boolean remote : new boolean[]{false, true}) {
            TraceState incoming = state(value);
            SamplingResult result = sample(ratio(0.25, true), parent(remote, true, incoming), KEPT);
            assertEquals(SamplingDecision.RECORD_AND_SAMPLE, result.getDecision());
            assertNull(result.getAttributes().get(RATIO));
            assertEquals(incoming, result.getUpdatedTraceState(incoming));
        }
    }

    @Test void upstreamSampledParentWithoutRatioNeverGetsOurRatio() throws Exception {
        for (boolean remote : new boolean[]{false, true}) {
            TraceState incoming = state(null);
            SamplingResult result = sample(ratio(0.25, true), parent(remote, true, incoming), KEPT);
            assertNull(result.getAttributes().get(RATIO));
            assertEquals(incoming, result.getUpdatedTraceState(incoming));
        }
    }

    @Test void nonParentBasedUsesOwnRatioAndReplacesOnlyFi() throws Exception {
        for (boolean remote : new boolean[]{false, true}) for (boolean sampled : new boolean[]{false, true}) {
            TraceState incoming = state("r:0.5");
            SamplingResult result = sample(ratio(0.25, false), parent(remote, sampled, incoming), KEPT);
            assertEquals(SamplingDecision.RECORD_AND_SAMPLE, result.getDecision());
            assertEquals(Double.valueOf(0.25), result.getAttributes().get(RATIO));
            assertEquals("r:0.25", result.getUpdatedTraceState(incoming).get("fi"));
            assertEquals("preserved", result.getUpdatedTraceState(incoming).get("vendor"));
            assertEquals("r:0.5", incoming.get("fi"), "parent state must not be mutated");
        }
    }

    @Test void invalidParentContextStillUsesRootDecision() throws Exception {
        TraceState incoming = state("r:0.5");
        SpanContext invalid = SpanContext.create("00000000000000000000000000000000", SPAN_ID, TraceFlags.getSampled(), incoming);
        SamplingResult result = sample(ratio(0.25, true), Context.root().with(Span.wrap(invalid)), KEPT);
        assertEquals(Double.valueOf(0.25), result.getAttributes().get(RATIO));
        assertEquals("r:0.25", result.getUpdatedTraceState(incoming).get("fi"));
        assertEquals("preserved", result.getUpdatedTraceState(incoming).get("vendor"));
    }

    @Test void disabledDefaultAndUnknownSamplersAreNotWrappedOrStamped() throws Exception {
        for (Map<String, String> environment : List.of(Map.<String, String>of(),
                Map.of("FI_SAMPLE_RATE", "1"), Map.of("FI_SAMPLE_RATE", "0"),
                Map.of("OTEL_TRACES_SAMPLER", "always_on"), Map.of("OTEL_TRACES_SAMPLER", "always_off"),
                Map.of("OTEL_TRACES_SAMPLER", "parentbased_always_on"), Map.of("OTEL_TRACES_SAMPLER", "parentbased_always_off"),
                Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "1"))) {
            Sampler selected = TraceConfig.samplerFromEnvironment(environment);
            assertSame(selected, owned(selected));
            for (Context context : List.of(Context.root(), parent(true, true, state("r:0.5")))) {
                SamplingResult result = sample(owned(selected), context, KEPT);
                assertNull(result.getAttributes().get(RATIO));
                assertEquals(state(null), result.getUpdatedTraceState(state(null)));
            }
        }
        for (Sampler custom : List.of(Sampler.traceIdRatioBased(0.25), Sampler.parentBased(Sampler.traceIdRatioBased(0.25)),
                delegate(SamplingResult.recordAndSample()))) {
            assertSame(custom, owned(custom), "never infer a custom sampler's ratio from its description");
            assertNull(sample(owned(custom), Context.root(), KEPT).getAttributes().get(RATIO));
        }
    }

    @ParameterizedTest @ValueSource(strings = {"NaN", "Infinity", "-0.1", "1.1", "garbage"})
    void invalidConfiguredRatiosNeverStamp(String value) throws Exception {
        for (Map<String, String> environment : List.of(Map.of("FI_SAMPLE_RATE", value),
                Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", value))) {
            SamplingResult result = sample(owned(TraceConfig.samplerFromEnvironment(environment)), Context.root(), KEPT);
            assertNull(result.getAttributes().get(RATIO));
            assertNull(result.getUpdatedTraceState(TraceState.getDefault()).get("fi"));
        }
    }

    @Test void factoryMetadataAloneDoesNotStampAndActivationIsIdempotent() throws Exception {
        Sampler selected = TraceConfig.samplerFromEnvironment(Map.of("FI_SAMPLE_RATE", "0.25"));
        assertNull(sample(selected, Context.root(), KEPT).getAttributes().get(RATIO));
        Sampler wrapped = owned(selected);
        assertSame(wrapped, owned(wrapped));
        assertEquals(selected.getDescription(), wrapped.getDescription());
        assertEquals(Double.valueOf(0.25), sample(wrapped, Context.root(), KEPT).getAttributes().get(RATIO));
    }

    @Test void decisionsMatchOriginalSamplerForEveryParentAndTraceId() throws Exception {
        for (boolean parentBased : new boolean[]{false, true}) {
            Sampler raw = Sampler.traceIdRatioBased(0.25);
            if (parentBased) raw = Sampler.parentBased(raw);
            Sampler wrapped = ratio(0.25, parentBased);
            for (Context parent : List.of(Context.root(), parent(false, true, state("r:0.5")),
                    parent(true, true, state(null)), parent(false, false, state("r:0.5")), parent(true, false, state(null)))) {
                for (String traceId : List.of(KEPT, DROPPED)) {
                    SamplingResult result = sample(wrapped, parent, traceId);
                    assertEquals(sample(raw, parent, traceId).getDecision(), result.getDecision());
                    if (result.getDecision() != SamplingDecision.RECORD_AND_SAMPLE) {
                        assertNull(result.getAttributes().get(RATIO));
                        assertEquals(state(null), result.getUpdatedTraceState(state(null)));
                    }
                }
            }
        }
    }

    @Test void droppedSpansAreNotExported() throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(ratio(0.25, true))
                .setIdGenerator(ids(DROPPED)).addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            Span root = provider.get("test").spanBuilder("dropped").setNoParent().startSpan();
            assertFalse(root.isRecording()); assertFalse(root.getSpanContext().isSampled());
            root.end();
            assertTrue(exporter.getFinishedSpanItems().isEmpty());
        }
    }

    @Test void wrapperPreservesDelegateAttributesAndUpdatedTraceState() throws Exception {
        AttributeKey<String> key = AttributeKey.stringKey("delegate.attribute");
        for (SamplingDecision decision : SamplingDecision.values()) {
            SamplingResult original = new SamplingResult() {
                @Override public SamplingDecision getDecision() { return decision; }
                @Override public Attributes getAttributes() { return Attributes.of(key, "retained"); }
                @Override public TraceState getUpdatedTraceState(TraceState parentState) {
                    return parentState.toBuilder().put("delegate", "updated").build();
                }
            };
            SamplingResult result = sample(wrapper(delegate(original), 0.25, false), Context.root(), KEPT);
            assertEquals(decision, result.getDecision());
            assertEquals("retained", result.getAttributes().get(key));
            TraceState updated = result.getUpdatedTraceState(state("r:0.5"));
            assertEquals("updated", updated.get("delegate"));
            assertEquals("preserved", updated.get("vendor"));
            if (decision == SamplingDecision.RECORD_AND_SAMPLE) {
                assertEquals(Double.valueOf(0.25), result.getAttributes().get(RATIO));
                assertEquals("r:0.25", updated.get("fi"));
            } else {
                assertSame(original, result);
                assertNull(result.getAttributes().get(RATIO));
                assertEquals("r:0.5", updated.get("fi"));
            }
        }
    }

    private static Sampler wrapper(Sampler delegate, double rate, boolean parentBased) throws Exception {
        var constructor = Class.forName("ai.traceai.SamplingRatioSampler")
            .getDeclaredConstructor(Sampler.class, double.class, boolean.class);
        constructor.setAccessible(true);
        return (Sampler) constructor.newInstance(delegate, rate, parentBased);
    }

    private static Sampler delegate(SamplingResult result) {
        return new Sampler() {
            @Override public SamplingResult shouldSample(Context parent, String traceId, String name, SpanKind kind,
                    Attributes attributes, List<LinkData> links) { return result; }
            @Override public String getDescription() { return "TraceIdRatioBased{0.250000}"; }
        };
    }

    private static IdGenerator ids(String traceId) {
        return new IdGenerator() {
            @Override public String generateSpanId() { return SPAN_ID; }
            @Override public String generateTraceId() { return traceId; }
        };
    }
}
