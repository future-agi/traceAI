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

    internal static Sampler Create(double? rate = null, Action<string>? warn = null)
    {
        if (rate.HasValue) return new ParentBasedSampler(new TraceIdRatioBasedSampler(rate.Value));

        // Match the .NET SDK: blank is unset, but nonblank names must not be trimmed.
        var name = Environment.GetEnvironmentVariable("OTEL_TRACES_SAMPLER")?.ToLowerInvariant();
        if (string.IsNullOrWhiteSpace(name)) return Default();
        switch (name)
        {
            case "always_on": return new AlwaysOnSampler();
            case "always_off": return new AlwaysOffSampler();
            case "parentbased_always_on": return new ParentBasedSampler(new AlwaysOnSampler());
            case "parentbased_always_off": return new ParentBasedSampler(new AlwaysOffSampler());
            case "traceidratio" or "parentbased_traceidratio":
                var arg = Environment.GetEnvironmentVariable("OTEL_TRACES_SAMPLER_ARG");
                double probability = 1;
                if (string.IsNullOrWhiteSpace(arg) ||
                    (double.TryParse(arg, NumberStyles.Float, CultureInfo.InvariantCulture, out probability) &&
                     double.IsFinite(probability) && probability >= 0 && probability <= 1))
                {
                    Sampler ratio = new TraceIdRatioBasedSampler(probability);
                    return name == "traceidratio" ? ratio : new ParentBasedSampler(ratio);
                }
                break;
        }

        // Do not echo environment values. Selection happens once per provider, never per span.
        const string message = "Invalid or unsupported OTEL_TRACES_SAMPLER/OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIdRatio(1.0)).";
        try { if (warn != null) warn(message); else Console.Error.WriteLine("[traceAI] " + message); } catch { }
        return Default();
    }

    private static Sampler Default() => new ParentBasedSampler(new TraceIdRatioBasedSampler(1));
}
