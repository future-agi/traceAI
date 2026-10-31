using OpenTelemetry;

namespace FIInstrumentation.Context;

/// <summary>
/// Suppresses new FI spans and OpenTelemetry instrumentation within the current async scope.
/// Nested scopes restore the previous suppression state when disposed.
/// </summary>
public sealed class SuppressTracing : IDisposable
{
    private readonly IDisposable _scope = SuppressInstrumentationScope.Begin();

    internal static bool IsSuppressed => Sdk.SuppressInstrumentation;

    public void Dispose() => _scope.Dispose();
}
