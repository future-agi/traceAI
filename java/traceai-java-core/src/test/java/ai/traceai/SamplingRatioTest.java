package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.TraceFlags;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
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

import java.util.ArrayList;
import java.util.HashMap;
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

    private static List<Map<String, String>> parentBasedEnvironments() {
        return List.of(Map.<String, String>of(), Map.of("FI_SAMPLE_RATE", "1"), Map.of("FI_SAMPLE_RATE", "0"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_always_on"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_always_off"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0.25"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "1"));
    }

    @Test void everyOwnedParentBasedSamplerExportsUpstreamRatioAndLocalChild() throws Exception {
        for (Map<String, String> environment : parentBasedEnvironments()) {
            for (boolean remote : new boolean[]{false, true}) {
                InMemorySpanExporter exporter = InMemorySpanExporter.create();
                TraceState incoming = state("r:0.5");
                Sampler selected = TraceConfig.samplerFromEnvironment(environment);
                // Provenance alone must not activate stamping on application-owned providers.
                assertNull(sample(selected, parent(remote, true, incoming), KEPT).getAttributes().get(RATIO));
                try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(owned(selected))
                        .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
                    Span first = provider.get("test").spanBuilder("upstream-child")
                        .setParent(parent(remote, true, incoming)).startSpan();
                    Span child = provider.get("test").spanBuilder("local-child")
                        .setParent(Context.root().with(first)).startSpan();
                    child.end(); first.end();
                    assertEquals(2, exporter.getFinishedSpanItems().size(), environment.toString());
                    exporter.getFinishedSpanItems().forEach(span -> {
                        assertEquals(Double.valueOf(0.5), span.getAttributes().get(RATIO), environment.toString());
                        assertEquals(incoming, span.getSpanContext().getTraceState());
                        assertEquals(KEPT, span.getTraceId());
                    });
                    assertEquals(first.getSpanContext().getSpanId(), exporter.getFinishedSpanItems().get(0).getParentSpanId());
                }
            }
        }
    }

    @Test void parentBasedBoundaryRootsNeverInventRatioAndUnsampledParentsNeverExport() throws Exception {
        for (Map<String, String> environment : parentBasedEnvironments()) {
            Sampler sampler = owned(TraceConfig.samplerFromEnvironment(environment));
            boolean fractional = "0.25".equals(environment.get("OTEL_TRACES_SAMPLER_ARG"));
            if (!fractional) {
                SamplingResult root = sample(sampler, Context.root(), KEPT);
                assertNull(root.getAttributes().get(RATIO), environment.toString());
                assertNull(root.getUpdatedTraceState(TraceState.getDefault()).get("fi"));
            }
            for (boolean remote : new boolean[]{false, true}) {
                InMemorySpanExporter exporter = InMemorySpanExporter.create();
                try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(sampler)
                        .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
                    Span span = provider.get("test").spanBuilder("unsampled-child")
                        .setParent(parent(remote, false, state("r:0.5"))).startSpan();
                    assertFalse(span.isRecording()); assertFalse(span.getSpanContext().isSampled());
                    span.end();
                    assertTrue(exporter.getFinishedSpanItems().isEmpty(), environment.toString());
                }
                SamplingResult noMetadata = sample(sampler, parent(remote, true, state(null)), KEPT);
                assertNull(noMetadata.getAttributes().get(RATIO));
                assertEquals(state(null), noMetadata.getUpdatedTraceState(state(null)));
            }
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"r:2", "r:abc", "r:0", "r:1", "r:-0.1", "r:NaN", "r:Infinity", "r:-Infinity", "other:0.5", "r:", "r: 0.5", "r:0x1.0p-1", "r:0.5d", "r:1e999", "r:1e-999", "r:0.5:extra"})
    void malformedParentRatioIsNotStamped(String value) throws Exception {
        for (Map<String, String> environment : parentBasedEnvironments()) for (boolean remote : new boolean[]{false, true}) {
            TraceState incoming = state(value);
            SamplingResult result = sample(owned(TraceConfig.samplerFromEnvironment(environment)), parent(remote, true, incoming), KEPT);
            assertEquals(SamplingDecision.RECORD_AND_SAMPLE, result.getDecision());
            assertNull(result.getAttributes().get(RATIO));
            assertEquals(incoming, result.getUpdatedTraceState(incoming));
        }
    }

    @ParameterizedTest @ValueSource(strings = {"r:0.5", "r:.5", "r:5e-1", "r:0.12345678901234568", "r:1e-10"})
    void defaultParentBasedCopiesOnlyValidPartialRatios(String value) throws Exception {
        for (boolean remote : new boolean[]{false, true}) {
            TraceState incoming = state(value);
            SamplingResult result = sample(owned(TraceConfig.samplerFromEnvironment(Map.of())), parent(remote, true, incoming), KEPT);
            assertEquals(Double.valueOf(value.substring(2)), result.getAttributes().get(RATIO));
            assertEquals(incoming, result.getUpdatedTraceState(incoming));
        }
    }

    @Test void copyOnlyWrapperPreservesDropAndRecordOnlyResults() throws Exception {
        for (SamplingResult original : List.of(SamplingResult.drop(), SamplingResult.recordOnly())) {
            for (boolean remote : new boolean[]{false, true}) {
                TraceState incoming = state("r:0.5");
                SamplingResult result = sample(wrapper(delegate(original), 1, true), parent(remote, true, incoming), KEPT);
                assertSame(original, result);
                assertNull(result.getAttributes().get(RATIO));
                assertEquals(incoming, result.getUpdatedTraceState(incoming));
            }
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

    @Test void nonParentBasedReplacesFiInAFullTraceState() throws Exception {
        // W3C tracestate holds at most 32 entries; replacing an existing fi entry must not need a free slot.
        var builder = TraceState.builder().put("fi", "r:0.5");
        for (int i = 0; i < 31; i++) builder.put("vendor" + i, "v" + i);
        TraceState incoming = builder.build();
        assertEquals(32, incoming.size());
        SamplingResult result = sample(ratio(0.25, false), parent(true, true, incoming), KEPT);
        assertEquals(SamplingDecision.RECORD_AND_SAMPLE, result.getDecision());
        assertEquals(Double.valueOf(0.25), result.getAttributes().get(RATIO));
        TraceState updated = result.getUpdatedTraceState(incoming);
        assertEquals("r:0.25", updated.get("fi"));
        assertEquals(32, updated.size());
        for (int i = 0; i < 31; i++) assertEquals("v" + i, updated.get("vendor" + i));
    }

    // Wire-level helpers: W3C header order makes "right-most" unambiguous.
    private static List<String> members(int count, boolean withFi) {
        List<String> members = new ArrayList<>();
        for (int i = 0; i < count; i++) members.add("vendor" + i + "=v" + i);
        if (withFi) members.add(count / 2, "fi=r:0.5");
        return members;
    }

    private static Context extract(List<String> members) {
        Map<String, String> carrier = Map.of(
            "traceparent", "00-" + KEPT + "-" + SPAN_ID + "-01", "tracestate", String.join(",", members));
        Context context = W3CTraceContextPropagator.getInstance().extract(Context.root(), carrier, new io.opentelemetry.context.propagation.TextMapGetter<Map<String, String>>() {
            @Override public Iterable<String> keys(Map<String, String> c) { return c.keySet(); }
            @Override public String get(Map<String, String> c, String key) { return c == null ? null : c.get(key); }
        });
        assertEquals(members.size(), Span.fromContext(context).getSpanContext().getTraceState().size());
        return context;
    }

    private static List<String> exportedMembers(Sampler sampler, Context incoming) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(sampler).setIdGenerator(ids(KEPT))
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            Span span = provider.get("test").spanBuilder("span").setParent(incoming).startSpan();
            assertTrue(span.getSpanContext().isSampled());
            Map<String, String> carrier = new HashMap<>();
            W3CTraceContextPropagator.getInstance().inject(Context.root().with(span), carrier, Map::put);
            span.end();
            assertEquals(Double.valueOf(0.25), exporter.getFinishedSpanItems().get(0).getAttributes().get(RATIO));
            String header = carrier.get("tracestate");
            return header == null ? List.of() : List.of(header.split(","));
        }
    }

    @Test void ownDecisionEvictsTheRightMostMemberToAddFiToAFullTraceState() throws Exception {
        List<String> incoming = members(32, false);
        List<String> expected = new ArrayList<>();
        expected.add("fi=r:0.25");
        expected.addAll(incoming.subList(0, 31));
        assertEquals(expected, exportedMembers(ratio(0.25, false), extract(incoming)));
    }

    @Test void ownDecisionReplacesFiInAFullTraceStateWithoutEviction() throws Exception {
        List<String> incoming = members(31, true);
        assertEquals(32, incoming.size());
        List<String> exported = exportedMembers(ratio(0.25, false), extract(incoming));
        assertEquals(32, exported.size());
        assertTrue(exported.contains("fi=r:0.25"));
        assertEquals("fi=r:0.25", exported.get(0), "the replaced fi moves to the left");
        List<String> others = new ArrayList<>(exported);
        others.remove("fi=r:0.25");
        assertEquals(members(31, false), others, "every other member is kept, in order");
    }

    @Test void ownDecisionAddsFiToA31MemberTraceStateWithoutEviction() throws Exception {
        List<String> incoming = members(31, false);
        List<String> expected = new ArrayList<>();
        expected.add("fi=r:0.25");
        expected.addAll(incoming);
        assertEquals(expected, exportedMembers(ratio(0.25, false), extract(incoming)));
    }

    @Test void parentBasedRootWithAFullInvalidParentStateWritesOnlyFi() throws Exception {
        TraceState full = Span.fromContext(extract(members(32, false))).getSpanContext().getTraceState();
        Context invalid = Context.root().with(Span.wrap(SpanContext.createFromRemoteParent(
            "00000000000000000000000000000000", SPAN_ID, TraceFlags.getSampled(), full)));
        assertEquals(List.of("fi=r:0.25"), exportedMembers(ratio(0.25, true), invalid));
    }

    @Test void parentBasedChildCopiesAFullParentStateUnchanged() throws Exception {
        List<String> incoming = members(31, true);
        Context parent = extract(incoming);
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(ratio(0.25, true)).setIdGenerator(ids(KEPT))
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            Span span = provider.get("test").spanBuilder("child").setParent(parent).startSpan();
            span.end();
            assertEquals(Span.fromContext(parent).getSpanContext().getTraceState(), span.getSpanContext().getTraceState());
            assertEquals(Double.valueOf(0.5), exporter.getFinishedSpanItems().get(0).getAttributes().get(RATIO));
        }
    }

    private static Context invalidParent(boolean invalidTraceId, boolean remote, boolean sampled) {
        String traceId = invalidTraceId ? "00000000000000000000000000000000" : KEPT;
        String spanId = invalidTraceId ? SPAN_ID : "0000000000000000";
        TraceFlags flags = sampled ? TraceFlags.getSampled() : TraceFlags.getDefault();
        SpanContext invalid = remote ? SpanContext.createFromRemoteParent(traceId, spanId, flags, state("r:0.5"))
            : SpanContext.create(traceId, spanId, flags, state("r:0.5"));
        assertFalse(invalid.isValid());
        assertEquals(state("r:0.5"), invalid.getTraceState(), "the public API retains invalid-context state");
        return Context.root().with(Span.wrap(invalid));
    }

    private static List<Map<String, String>> fullRateEnvironments() {
        return List.of(Map.<String, String>of(), Map.of("FI_SAMPLE_RATE", "1"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_always_on"),
            Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "1"),
            Map.of("OTEL_TRACES_SAMPLER", "always_on"),
            Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "1"));
    }

    @Test void invalidParentContextStillUsesRootDecisionButResetsAllInheritedState() throws Exception {
        TraceState incoming = state("r:0.5");
        SamplingResult result = sample(ratio(0.25, true), invalidParent(true, false, true), KEPT);
        assertEquals(Double.valueOf(0.25), result.getAttributes().get(RATIO));
        assertEquals(TraceState.builder().put("fi", "r:0.25").build(), result.getUpdatedTraceState(incoming));
        assertEquals(state("r:0.5"), incoming, "the invalid context itself is not mutated");
    }

    @Test void invalidParentsExportCleanFullRateRootsAndLocalChildren() throws Exception {
        for (Map<String, String> environment : fullRateEnvironments()) {
            for (boolean invalidTraceId : new boolean[]{false, true}) for (boolean remote : new boolean[]{false, true}) {
                for (boolean sampled : new boolean[]{false, true}) {
                    assertExportedPair(owned(TraceConfig.samplerFromEnvironment(environment)),
                        invalidParent(invalidTraceId, remote, sampled), null, TraceState.getDefault());
                }
            }
        }
    }

    @Test void invalidParentsExportOnlyTheLocalPartialRatioOnRootsAndChildren() throws Exception {
        for (Map<String, String> environment : List.of(Map.of("FI_SAMPLE_RATE", "0.25"),
                Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0.25"),
                Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0.25"))) {
            for (boolean invalidTraceId : new boolean[]{false, true}) for (boolean remote : new boolean[]{false, true}) {
                for (boolean sampled : new boolean[]{false, true}) {
                    assertExportedPair(owned(TraceConfig.samplerFromEnvironment(environment)),
                        invalidParent(invalidTraceId, remote, sampled), 0.25, TraceState.builder().put("fi", "r:0.25").build());
                }
            }
        }
    }

    @Test void noParentFullRateRootsAndChildrenStayUnweighted() throws Exception {
        for (Map<String, String> environment : fullRateEnvironments()) {
            assertExportedPair(owned(TraceConfig.samplerFromEnvironment(environment)), Context.root(), null, TraceState.getDefault());
        }
    }

    @Test void invalidDroppedRootsAndTheirLocalChildrenHaveNoInheritedState() throws Exception {
        for (Map<String, String> environment : List.of(Map.of("FI_SAMPLE_RATE", "0"),
                Map.of("OTEL_TRACES_SAMPLER", "parentbased_always_off"), Map.of("OTEL_TRACES_SAMPLER", "always_off"),
                Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0"),
                Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0"),
                Map.of("FI_SAMPLE_RATE", "0.25"))) {
            for (boolean invalidTraceId : new boolean[]{false, true}) for (boolean remote : new boolean[]{false, true}) {
                for (boolean sampled : new boolean[]{false, true}) {
                    InMemorySpanExporter exporter = InMemorySpanExporter.create();
                    try (SdkTracerProvider provider = SdkTracerProvider.builder()
                            .setSampler(owned(TraceConfig.samplerFromEnvironment(environment))).setIdGenerator(ids(DROPPED))
                            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
                        Span root = provider.get("test").spanBuilder("dropped-root")
                            .setParent(invalidParent(invalidTraceId, remote, sampled)).startSpan();
                        Span child = provider.get("test").spanBuilder("dropped-child").setParent(Context.root().with(root)).startSpan();
                        for (Span span : List.of(root, child)) {
                            assertFalse(span.isRecording()); assertFalse(span.getSpanContext().isSampled());
                            assertEquals(TraceState.getDefault(), span.getSpanContext().getTraceState(), environment.toString());
                            span.end();
                        }
                        assertTrue(exporter.getFinishedSpanItems().isEmpty());
                    }
                }
            }
        }
    }

    @Test void applicationFactoryAndUnknownCustomSamplersRetainInvalidContextState() throws Exception {
        for (Map<String, String> environment : fullRateEnvironments()) {
            // A factory-selected sampler on an application provider has provenance but no activation.
            assertExportedPair(TraceConfig.samplerFromEnvironment(environment), invalidParent(true, true, true), null, state("r:0.5"));
        }
        for (Sampler custom : List.of(Sampler.alwaysOn(), Sampler.parentBased(Sampler.alwaysOn()),
                delegate(SamplingResult.recordAndSample()))) {
            assertSame(custom, owned(custom));
            assertExportedPair(owned(custom), invalidParent(false, false, true), null, state("r:0.5"));
        }
        for (SamplingResult original : List.of(SamplingResult.drop(), SamplingResult.recordOnly())) {
            Sampler custom = delegate(original);
            assertSame(custom, owned(custom));
            SamplingResult result = sample(owned(custom), invalidParent(true, true, true), KEPT);
            assertSame(original, result);
            assertEquals(state("r:0.5"), result.getUpdatedTraceState(state("r:0.5")));
        }
    }

    private static void assertExportedPair(Sampler sampler, Context incoming, Double expectedRatio, TraceState expectedState) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(sampler).setIdGenerator(ids(KEPT))
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            Span root = provider.get("test").spanBuilder("new-root").setParent(incoming).startSpan();
            Span child = provider.get("test").spanBuilder("local-child").setParent(Context.root().with(root)).startSpan();
            child.end(); root.end();
            var spans = exporter.getFinishedSpanItems();
            assertEquals(2, spans.size());
            for (var span : spans) {
                assertTrue(span.getSpanContext().isValid()); assertTrue(span.getSpanContext().isSampled());
                assertEquals(expectedRatio, span.getAttributes().get(RATIO), sampler.getDescription());
                assertEquals(expectedState, span.getSpanContext().getTraceState(), sampler.getDescription());
            }
            assertEquals(root.getSpanContext().getTraceId(), spans.get(0).getTraceId());
            assertEquals(root.getSpanContext().getSpanId(), spans.get(0).getParentSpanId());
            assertFalse(spans.get(1).getParentSpanContext().isValid(), "the first span really is a new root");
        }
    }

    @Test void nonParentBasedFullRateAndDisabledSamplersNeverStampAndUnknownSamplersAreNotWrapped() throws Exception {
        for (Map<String, String> environment : List.of(
                Map.of("OTEL_TRACES_SAMPLER", "always_on"), Map.of("OTEL_TRACES_SAMPLER", "always_off"),
                Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0"),
                Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", "1"))) {
            Sampler selected = TraceConfig.samplerFromEnvironment(environment);
            assertNotSame(selected, owned(selected), "known boundary samplers also enforce invalid-root state reset");
            assertEquals(selected.getDescription(), owned(selected).getDescription());
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
            SamplingResult result = sample(wrapper(delegate(original), 0.25, false), parent(false, true, state("r:0.5")), KEPT);
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

    @Test void invalidRootStateResetPreservesDelegateCallDecisionAttributesAndNewState() throws Exception {
        AttributeKey<String> key = AttributeKey.stringKey("delegate.attribute");
        for (SamplingDecision decision : SamplingDecision.values()) {
            Context incoming = io.opentelemetry.api.baggage.Baggage.builder().put("test-baggage", "retained").build()
                .storeInContext(invalidParent(true, true, true));
            Attributes originalAttributes = Attributes.of(key, "retained");
            SamplingResult original = new SamplingResult() {
                @Override public SamplingDecision getDecision() { return decision; }
                @Override public Attributes getAttributes() { return originalAttributes; }
                @Override public TraceState getUpdatedTraceState(TraceState parentState) {
                    assertEquals(TraceState.getDefault(), parentState, "delegate updates start from clean root state");
                    return parentState.toBuilder().put("delegate", "updated").build();
                }
            };
            Sampler delegate = new Sampler() {
                @Override public SamplingResult shouldSample(Context context, String traceId, String name, SpanKind kind,
                        Attributes attributes, List<LinkData> links) {
                    assertSame(incoming, context, "do not replace the caller's context to clear tracestate");
                    assertEquals("retained", io.opentelemetry.api.baggage.Baggage.fromContext(context).getEntryValue("test-baggage"));
                    return original;
                }
                @Override public String getDescription() { return "test-delegate"; }
            };
            SamplingResult result = sample(wrapper(delegate, 0.25, true), incoming, KEPT);
            assertEquals(decision, result.getDecision());
            assertEquals("retained", result.getAttributes().get(key));
            TraceState updated = result.getUpdatedTraceState(state("r:0.5"));
            assertEquals("updated", updated.get("delegate"));
            assertNull(updated.get("vendor"));
            if (decision == SamplingDecision.RECORD_AND_SAMPLE) {
                assertEquals(Double.valueOf(0.25), result.getAttributes().get(RATIO));
                assertEquals("r:0.25", updated.get("fi"));
            } else {
                assertSame(originalAttributes, result.getAttributes());
                assertNull(result.getAttributes().get(RATIO));
                assertNull(updated.get("fi"));
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
            private long nextSpanId = 2;
            @Override public String generateSpanId() { return String.format("%016x", nextSpanId++); }
            @Override public String generateTraceId() { return traceId; }
        };
    }
}
