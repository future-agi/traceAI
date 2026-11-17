using System.Globalization;
using System.Runtime.CompilerServices;
using OpenTelemetry.Trace;

[assembly: InternalsVisibleTo("TraceAI.AutoInstrumentation.Runtime")]

namespace FIInstrumentation;

/// <summary>Shared by manual registration and the private .NET auto-agent SDK build.</summary>
internal static class SamplingConfiguration
{
    // Keep FI's existing invalid-value fallback and finite-rate clamping. Null alone
    // means unset; do not let a defaulted rate hide the standard OTel environment.
    internal static double? ParseRate(string? value) => value == null ? null :
        double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var rate) && double.IsFinite(rate)
            ? Math.Clamp(rate, 0, 1) : 1;

    // Identity, not descriptions or reflection over SDK internals, establishes provenance.
    private sealed record Ratio(double Rate, bool ParentBased);
    private static readonly ConditionalWeakTable<Sampler, Ratio> Ratios = new();
    private static Sampler CreateRatio(double rate, bool parentBased) =>
        CreateKnown(new TraceIdRatioBasedSampler(rate), rate, parentBased);

    private static Sampler CreateKnown(Sampler sampler, double rate, bool parentBased)
    {
        if (parentBased) sampler = new ParentBasedSampler(sampler);
        Ratios.Add(sampler, new(rate, parentBased));
        return sampler;
    }

    internal static Sampler Wrap(Sampler sampler) =>
        Ratios.TryGetValue(sampler, out var ratio) && (ratio.ParentBased || RatioSampler.Valid(ratio.Rate))
            ? new RatioSampler(sampler, ratio.Rate, ratio.ParentBased) : sampler;

    internal static Sampler Create(double? rate = null, Action<string>? warn = null)
    {
        if (rate.HasValue) return CreateRatio(rate.Value, true);

        // Match the .NET SDK: blank is unset, but nonblank names must not be trimmed.
        var name = Environment.GetEnvironmentVariable("OTEL_TRACES_SAMPLER")?.ToLowerInvariant();
        if (string.IsNullOrWhiteSpace(name)) return Default();
        switch (name)
        {
            case "always_on": return new AlwaysOnSampler();
            case "always_off": return new AlwaysOffSampler();
            case "parentbased_always_on": return CreateKnown(new AlwaysOnSampler(), 1, true);
            case "parentbased_always_off": return CreateKnown(new AlwaysOffSampler(), 0, true);
            case "traceidratio" or "parentbased_traceidratio":
                var arg = Environment.GetEnvironmentVariable("OTEL_TRACES_SAMPLER_ARG");
                double probability = 1;
                if (string.IsNullOrWhiteSpace(arg) ||
                    (double.TryParse(arg, NumberStyles.Float, CultureInfo.InvariantCulture, out probability) &&
                     double.IsFinite(probability) && probability >= 0 && probability <= 1))
                {
                    return CreateRatio(probability, name == "parentbased_traceidratio");
                }
                break;
        }

        // Do not echo environment values. Selection happens once per provider, never per span.
        const string message = "Invalid or unsupported OTEL_TRACES_SAMPLER/OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIdRatio(1.0)).";
        try { if (warn != null) warn(message); else Console.Error.WriteLine("[traceAI] " + message); } catch { }
        return Default();
    }

    private static Sampler Default() => CreateRatio(1, true);
}

/// <summary>Adds head-sampling probability without changing the selected sampler's decision.</summary>
internal sealed class RatioSampler(Sampler inner, double rate, bool parentBased) : Sampler
{
    internal const string Attribute = "fi.sampling.ratio";
    internal static bool Valid(double value) => double.IsFinite(value) && value > 0 && value < 1;

    public override SamplingResult ShouldSample(in SamplingParameters parameters)
    {
        var result = inner.ShouldSample(in parameters);
        if (result.Decision != SamplingDecision.RecordAndSample) return result;

        double? effective = Valid(rate) ? rate : null;
        if (parentBased && parameters.ParentContext.TraceId != default)
        {
            // A parent-based child did not win our local ratio lottery. Inherit a
            // sampled parent's probability even when the local root rate is 0 or 1.
            effective = (parameters.ParentContext.TraceFlags & System.Diagnostics.ActivityTraceFlags.Recorded) != 0
                ? ReadRatio(parameters.ParentContext.TraceState) : null;
        }
        if (!effective.HasValue) return result;
        var attributes = (result.Attributes ?? []).Where(pair => pair.Key != Attribute).ToList();
        attributes.Add(new(Attribute, effective.Value));
        return new SamplingResult(result.Decision, attributes,
            WriteRatio(result.TraceStateString ?? parameters.ParentContext.TraceState, effective.Value));
    }

    internal static double? ReadRatio(string? state)
    {
        double? ratio = null;
        bool found = false;
        foreach (var entry in (state ?? "").Split(','))
        {
            var member = entry.Trim();
            var separator = member.IndexOf('=');
            if (separator < 0 || member[..separator].TrimEnd() != "fi") continue;
            if (found) return null;
            found = true;
            var value = member[(separator + 1)..];
            if (value.StartsWith("r:", StringComparison.Ordinal) &&
                double.TryParse(value.AsSpan(2), NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) && Valid(parsed))
                ratio = parsed;
        }
        return ratio;
    }

    internal static string WriteRatio(string? state, double ratio)
    {
        var entries = (state ?? "").Split(',').Select(entry => entry.Trim())
            .Where(entry => entry.Length > 0 && entry.Split('=', 2)[0].TrimEnd() != "fi");
        // W3C allows at most 32 members; inserting ours evicts the oldest if full.
        return string.Join(",", new[] { "fi=r:" + ratio.ToString("R", CultureInfo.InvariantCulture) }.Concat(entries).Take(32));
    }
}
