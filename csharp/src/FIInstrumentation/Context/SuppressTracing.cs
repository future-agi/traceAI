using System.Diagnostics;

namespace FIInstrumentation.Context;

/// <summary>
/// Suppresses tracing within a scope. All spans created while this is active will be dropped.
/// Uses the OpenTelemetry SuppressInstrumentationScope.
/// </summary>
public sealed class SuppressTracing : IDisposable
{
    private readonly ActivityListener _listener;
    private static readonly AsyncLocal<bool> s_suppressed = new();

    public SuppressTracing()
    {
        s_suppressed.Value = true;

        // Register a listener that prevents sampling when suppressed
        _listener = new ActivityListener
        {
            ShouldListenTo = _ => true,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) =>
                s_suppressed.Value
                    ? ActivitySamplingResult.None
                    : ActivitySamplingResult.AllDataAndRecorded,
        };
    }

    internal static bool IsSuppressed => s_suppressed.Value;

    public void Dispose()
    {
        s_suppressed.Value = false;
        _listener.Dispose();
    }
}
