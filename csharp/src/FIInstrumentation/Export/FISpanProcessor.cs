using System.Diagnostics;
using OpenTelemetry;

namespace FIInstrumentation.Export;

/// <summary>
/// Custom span processor that auto-sets OK status on unset spans before export.
/// Delegates to an inner BaseExporter for actual export.
/// </summary>
public class FISpanProcessor : BaseProcessor<Activity>
{
    public override void OnEnd(Activity data)
    {
        // Auto-set OK status if the span status was never explicitly set
        if (data.Status == ActivityStatusCode.Unset)
        {
            data.SetStatus(ActivityStatusCode.Ok);
        }

        base.OnEnd(data);
    }
}
