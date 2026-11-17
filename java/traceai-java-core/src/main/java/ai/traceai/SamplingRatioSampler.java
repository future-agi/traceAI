package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.trace.data.LinkData;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import io.opentelemetry.sdk.trace.samplers.SamplingDecision;
import io.opentelemetry.sdk.trace.samplers.SamplingResult;

import java.util.List;
import java.util.regex.Pattern;

/** Adds weighting metadata without changing the selected sampler's decision. */
final class SamplingRatioSampler implements Sampler {
    private static final AttributeKey<Double> RATIO = AttributeKey.doubleKey("fi.sampling.ratio");
    private static final String STATE_KEY = "fi";
    // Double.parseDouble alone also accepts Java hex literals, suffixes and surrounding whitespace.
    private static final Pattern DECIMAL = Pattern.compile("[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?");

    private final Sampler delegate;
    private final double ratio;
    private final boolean parentBased;

    SamplingRatioSampler(Sampler delegate, double ratio, boolean parentBased) {
        if (!Double.isFinite(ratio) || ratio < 0 || ratio > 1)
            throw new IllegalArgumentException("Root sampling ratio must be finite and between zero and one");
        this.delegate = delegate;
        this.ratio = ratio;
        this.parentBased = parentBased;
    }

    /** The factory records provenance, but does not enable attributes on application-owned providers. */
    static Sampler ratioSampler(double ratio, boolean parentBased) {
        Sampler sampler = Sampler.traceIdRatioBased(ratio);
        if (parentBased) return parentBasedSampler(sampler, ratio);
        return validRatio(ratio) ? new KnownRatioSampler(sampler, ratio, false) : sampler;
    }

    /** Parent inheritance is known even when the root sampler has no partial ratio to stamp. */
    static Sampler parentBasedSampler(Sampler rootSampler, double rootRatio) {
        return new KnownRatioSampler(Sampler.parentBased(rootSampler), rootRatio, true);
    }

    static Sampler forOwnedProvider(Sampler sampler) {
        if (!(sampler instanceof KnownRatioSampler)) return sampler;
        KnownRatioSampler known = (KnownRatioSampler) sampler;
        return new SamplingRatioSampler(known.delegate, known.ratio, known.parentBased);
    }

    @Override
    public SamplingResult shouldSample(Context parentContext, String traceId, String name, SpanKind spanKind,
            Attributes attributes, List<LinkData> parentLinks) {
        SamplingResult result = delegate.shouldSample(parentContext, traceId, name, spanKind, attributes, parentLinks);
        if (result.getDecision() != SamplingDecision.RECORD_AND_SAMPLE) return result;

        SpanContext parent = Span.fromContext(parentContext).getSpanContext();
        boolean ownDecision = !parentBased || !parent.isValid();
        Double effectiveRatio = ownDecision ? (validRatio(ratio) ? Double.valueOf(ratio) : null) : parentRatio(parent);
        if (effectiveRatio == null) return result;

        Attributes stamped = result.getAttributes().toBuilder().put(RATIO, effectiveRatio).build();
        return new SamplingResult() {
            @Override public SamplingDecision getDecision() { return result.getDecision(); }
            @Override public Attributes getAttributes() { return stamped; }
            @Override public TraceState getUpdatedTraceState(TraceState parentTraceState) {
                TraceState updated = result.getUpdatedTraceState(parentTraceState);
                // Preserve the delegate's state updates and all other vendors; inherited fi is unchanged.
                return ownDecision ? updated.toBuilder().put(STATE_KEY, "r:" + Double.toString(ratio)).build() : updated;
            }
        };
    }

    @Override public String getDescription() { return delegate.getDescription(); }

    private static Double parentRatio(SpanContext parent) {
        if (!parent.isSampled()) return null;
        String value = parent.getTraceState().get(STATE_KEY);
        if (value == null || !value.startsWith("r:")) return null;
        String decimal = value.substring(2);
        if (!DECIMAL.matcher(decimal).matches()) return null;
        try {
            double ratio = Double.parseDouble(decimal);
            return validRatio(ratio) ? ratio : null;
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    private static boolean validRatio(double ratio) {
        return Double.isFinite(ratio) && ratio > 0 && ratio < 1;
    }

    /** Private marker: never recognize custom samplers by description or by reflective inspection. */
    private static final class KnownRatioSampler implements Sampler {
        private final Sampler delegate;
        private final double ratio;
        private final boolean parentBased;

        private KnownRatioSampler(Sampler delegate, double ratio, boolean parentBased) {
            this.delegate = delegate;
            this.ratio = ratio;
            this.parentBased = parentBased;
        }

        @Override
        public SamplingResult shouldSample(Context parentContext, String traceId, String name, SpanKind spanKind,
                Attributes attributes, List<LinkData> parentLinks) {
            return delegate.shouldSample(parentContext, traceId, name, spanKind, attributes, parentLinks);
        }

        @Override public String getDescription() { return delegate.getDescription(); }
    }
}
