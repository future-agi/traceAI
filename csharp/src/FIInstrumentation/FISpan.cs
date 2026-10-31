using System.Diagnostics;
using System.Text.Json;
using FIInstrumentation.Types;

namespace FIInstrumentation;

/// <summary>
/// Wraps a System.Diagnostics.Activity (OTel span) with convenience methods and masking.
/// </summary>
public class FISpan : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = false,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    private const int MaxAttributeLength = 32_000;

    private readonly Activity _activity;
    private readonly TraceConfig _config;
    private readonly bool _recording;
    private bool _disposed;

    internal FISpan(Activity activity, TraceConfig config, bool recording = true)
    {
        _activity = activity ?? throw new ArgumentNullException(nameof(activity));
        _config = config;
        _recording = recording;
    }

    /// <summary>
    /// The underlying Activity (OTel span). For a suppressed span, this is an unstarted
    /// placeholder that does not become Activity.Current and must not be started.
    /// </summary>
    public Activity Activity => _activity;

    /// <summary>
    /// The span's trace ID.
    /// </summary>
    public string TraceId => _activity.TraceId.ToString();

    /// <summary>
    /// The span's ID.
    /// </summary>
    public string SpanId => _activity.SpanId.ToString();

    // ── Convenience setters ─────────────────────────────────────

    public void SetInput(object value, FIMimeType mimeType = FIMimeType.Text)
    {
        if (!_recording) return;

        var serialized = SerializeValue(value);
        var masked = _config.Mask(SemanticConventions.InputValue, serialized);
        if (masked != null)
        {
            SetAttribute(SemanticConventions.InputValue, Truncate(masked));
            SetAttribute(SemanticConventions.InputMimeType, mimeType.ToValue());
        }
    }

    public void SetOutput(object value, FIMimeType mimeType = FIMimeType.Text)
    {
        if (!_recording) return;

        var serialized = SerializeValue(value);
        var masked = _config.Mask(SemanticConventions.OutputValue, serialized);
        if (masked != null)
        {
            SetAttribute(SemanticConventions.OutputValue, Truncate(masked));
            SetAttribute(SemanticConventions.OutputMimeType, mimeType.ToValue());
        }
    }

    public void SetInputMessages(List<Dictionary<string, string>> messages)
    {
        if (!_recording) return;

        var json = SafeJsonSerialize(messages);
        var masked = _config.Mask(SemanticConventions.GenAiInputMessages, json);
        if (masked != null)
            SetAttribute(SemanticConventions.GenAiInputMessages, Truncate(masked));
    }

    public void SetOutputMessages(List<Dictionary<string, string>> messages)
    {
        if (!_recording) return;

        var json = SafeJsonSerialize(messages);
        var masked = _config.Mask(SemanticConventions.GenAiOutputMessages, json);
        if (masked != null)
            SetAttribute(SemanticConventions.GenAiOutputMessages, Truncate(masked));
    }

    public void SetTokenCounts(long inputTokens, long outputTokens, long totalTokens)
    {
        SetAttribute(SemanticConventions.GenAiUsageInputTokens, inputTokens);
        SetAttribute(SemanticConventions.GenAiUsageOutputTokens, outputTokens);
        SetAttribute(SemanticConventions.GenAiUsageTotalTokens, totalTokens);
    }

    public void SetTool(string name, string? description = null, object? parameters = null)
    {
        if (!_recording) return;

        SetAttribute(SemanticConventions.GenAiToolName, name);
        if (description != null)
            SetAttribute(SemanticConventions.GenAiToolDescription, description);
        if (parameters != null)
            SetAttribute(SemanticConventions.GenAiToolParameters, SafeJsonSerialize(parameters));
    }

    public void SetError(Exception ex)
    {
        if (!_recording) return;

        _activity.SetStatus(ActivityStatusCode.Error, ex.Message);
        SetAttribute(SemanticConventions.ErrorType, ex.GetType().FullName ?? ex.GetType().Name);
        SetAttribute(SemanticConventions.ErrorMessage, ex.Message);
        var errorEvent = new ActivityEvent("exception", tags: new ActivityTagsCollection
        {
            { "exception.type", ex.GetType().FullName },
            { "exception.message", ex.Message },
            { "exception.stacktrace", ex.StackTrace },
        });
        _activity.AddEvent(errorEvent);
    }

    // ── Generic attribute setters ───────────────────────────────

    public void SetAttribute(string key, string value)
    {
        if (_recording) _activity.SetTag(key, value);
    }

    public void SetAttribute(string key, long value)
    {
        if (_recording) _activity.SetTag(key, value);
    }

    public void SetAttribute(string key, double value)
    {
        if (_recording) _activity.SetTag(key, value);
    }

    public void SetAttribute(string key, bool value)
    {
        if (_recording) _activity.SetTag(key, value);
    }

    public void SetAttribute(string key, string[] values)
    {
        if (_recording) _activity.SetTag(key, values);
    }

    // ── IDisposable ─────────────────────────────────────────────

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        if (!_recording) return;

        // Auto-set OK status if unset
        if (_activity.Status == ActivityStatusCode.Unset)
            _activity.SetStatus(ActivityStatusCode.Ok);

        _activity.Stop();
        _activity.Dispose();
        GC.SuppressFinalize(this);
    }

    // ── Helpers ─────────────────────────────────────────────────

    private static string SerializeValue(object value)
    {
        if (value is string s)
            return s;

        return SafeJsonSerialize(value);
    }

    internal static string SafeJsonSerialize(object value)
    {
        try
        {
            return JsonSerializer.Serialize(value, JsonOptions);
        }
        catch
        {
            return value.ToString() ?? string.Empty;
        }
    }

    private static string Truncate(string value)
    {
        if (value.Length <= MaxAttributeLength)
            return value;
        return value[..MaxAttributeLength];
    }
}
