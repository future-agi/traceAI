using System.Diagnostics;
using System.Globalization;
using OpenTelemetry.Trace;

namespace FIInstrumentation.Tests;

[Collection("Sampler environment")]
public class SamplingRatioTests
{
    private const string Key = "fi.sampling.ratio";
    // The ratio sampler uses the low 64 bits; this trace is deterministically kept.
    private static readonly ActivityTraceId Kept = ActivityTraceId.CreateFromString("00000000000000010000000000000001");
    private static SamplingResult Sample(Sampler sampler, ActivityContext parent = default) =>
        sampler.ShouldSample(new SamplingParameters(parent, Kept, "test", ActivityKind.Internal));
    private static ActivityContext Parent(string? state, bool sampled = true, bool remote = true) =>
        new(Kept, ActivitySpanId.CreateRandom(), sampled ? ActivityTraceFlags.Recorded : ActivityTraceFlags.None, state, remote);
    private static object? Ratio(SamplingResult result) => result.Attributes?.FirstOrDefault(p => p.Key == Key).Value;

    [Fact]
    public void RootAndLocalChildCarryTheSameRatio()
    {
        var sampler = SamplingConfiguration.Wrap(SamplingConfiguration.Create(.25));
        var root = Sample(sampler);
        Assert.Equal(SamplingDecision.RecordAndSample, root.Decision);
        Assert.Equal(.25, Assert.IsType<double>(Ratio(root)));
        Assert.Equal("fi=r:0.25", root.TraceStateString);
        var child = Sample(sampler, Parent(root.TraceStateString, remote: false));
        Assert.Equal(.25, Ratio(child)); Assert.Equal(root.TraceStateString, child.TraceStateString);
    }

    [Theory]
    [InlineData("vendor=opaque,fi=r:0.5", .5)]
    [InlineData("vendor=opaque", null)]
    [InlineData(null, null)]
    [InlineData("fi=r:2", null)]
    [InlineData("fi=r:abc", null)]
    [InlineData("fi=r:NaN", null)]
    [InlineData("fi=r:Infinity", null)]
    [InlineData("fi=r:0", null)]
    [InlineData("fi=r:1", null)]
    [InlineData("fi=r:-0.25", null)]
    [InlineData("other=r:0.5", null)]
    public void ParentBasedUsesOnlyValidUpstreamRatio(string? state, double? expected)
    {
        var result = Sample(SamplingConfiguration.Wrap(SamplingConfiguration.Create(.25)), Parent(state));
        Assert.Equal(SamplingDecision.RecordAndSample, result.Decision);
        Assert.Equal(expected, Ratio(result));
        if (expected == null) Assert.Equal(Sample(SamplingConfiguration.Create(.25), Parent(state)).TraceStateString, result.TraceStateString);
        else { Assert.Contains("fi=r:0.5", result.TraceStateString); Assert.Contains("vendor=opaque", result.TraceStateString); }
    }

    [Theory]
    [InlineData(SamplingDecision.Drop)]
    [InlineData(SamplingDecision.RecordOnly)]
    [InlineData(SamplingDecision.RecordAndSample)]
    public void WrapperPreservesDecisionAttributesAndUpdatedTracestate(SamplingDecision decision)
    {
        var original = new SamplingResult(decision, new[] { new KeyValuePair<string, object>("custom", 42) }, "vendor=opaque,fi=r:0.9");
        var inner = new FixedSampler(original);
        var result = Sample(new RatioSampler(inner, .25, false), Parent("parent=state"));
        Assert.Equal(decision, result.Decision); Assert.Equal(1, inner.Calls);
        Assert.Contains(result.Attributes, pair => pair.Key == "custom" && Equals(pair.Value, 42));
        if (decision == SamplingDecision.RecordAndSample)
        {
            Assert.Equal(.25, Ratio(result)); Assert.Equal("fi=r:0.25,vendor=opaque", result.TraceStateString);
        }
        else { Assert.Null(Ratio(result)); Assert.Equal(original.TraceStateString, result.TraceStateString); }
    }

    [Theory]
    [InlineData(0)] [InlineData(1)] [InlineData(double.NaN)] [InlineData(double.PositiveInfinity)]
    public void InvalidOrDisabledRatiosNeverStamp(double rate)
    {
        var result = Sample(new RatioSampler(new AlwaysOnSampler(), rate, false));
        Assert.Null(Ratio(result)); Assert.Null(result.TraceStateString);
    }

    [Fact]
    public void UnknownSamplersAreNotRecognizedByTypeOrDescription()
    {
        foreach (var sampler in new Sampler[] { new TraceIdRatioBasedSampler(.25), new ParentBasedSampler(new TraceIdRatioBasedSampler(.25)), new AlwaysOnSampler() })
        { Assert.Same(sampler, SamplingConfiguration.Wrap(sampler)); Assert.Null(Ratio(Sample(SamplingConfiguration.Wrap(sampler)))); }
    }

    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void WrappedDecisionsMatchSdkAcrossRootsAndParentFlags(bool parentBased)
    {
        Sampler inner = new TraceIdRatioBasedSampler(.25);
        if (parentBased) inner = new ParentBasedSampler(inner);
        var wrapped = new RatioSampler(inner, .25, parentBased);
        for (var i = 0; i < 256; i++)
        {
            var trace = ActivityTraceId.CreateRandom();
            foreach (var parent in new[] { default(ActivityContext), Parent("fi=r:0.5"), Parent(null, false), Parent("fi=r:0.5", remote: false), Parent(null, false, false) })
            {
                var parameters = new SamplingParameters(parent, trace, "parity", ActivityKind.Internal);
                var original = inner.ShouldSample(in parameters); var result = wrapped.ShouldSample(in parameters);
                Assert.Equal(original.Decision, result.Decision);
                if (result.Decision != SamplingDecision.RecordAndSample) Assert.Null(Ratio(result));
                else if (!parentBased || parent.TraceId == default) Assert.Equal(.25, Ratio(result));
                else Assert.Equal(.5, Ratio(result));
            }
        }
    }

    [Fact]
    public void RatioFormattingIsInvariantAndRoundTrips()
    {
        var previous = CultureInfo.CurrentCulture;
        try
        {
            CultureInfo.CurrentCulture = CultureInfo.GetCultureInfo("fr-FR");
            const double rate = .12345678901234567;
            var result = Sample(new RatioSampler(new AlwaysOnSampler(), rate, false));
            Assert.Equal("fi=r:" + rate.ToString("R", CultureInfo.InvariantCulture), result.TraceStateString);
            Assert.Equal(rate, Ratio(Sample(new RatioSampler(new AlwaysOnSampler(), .25, true), Parent(result.TraceStateString))));
        }
        finally { CultureInfo.CurrentCulture = previous; }
    }

    private sealed class FixedSampler(SamplingResult result) : Sampler
    {
        internal int Calls;
        public override SamplingResult ShouldSample(in SamplingParameters parameters) { Calls++; return result; }
    }
}
