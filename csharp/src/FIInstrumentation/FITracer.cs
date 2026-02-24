using System.Diagnostics;
using FIInstrumentation.Context;

namespace FIInstrumentation;

/// <summary>
/// Core tracer with span creation, lambda-based tracing, and convenience shortcuts.
/// Wraps a System.Diagnostics.ActivitySource (OTel tracer).
/// </summary>
public class FITracer
{
    private readonly ActivitySource _activitySource;
    private readonly TraceConfig _config;

    internal FITracer(ActivitySource activitySource, TraceConfig config)
    {
        _activitySource = activitySource ?? throw new ArgumentNullException(nameof(activitySource));
        _config = config;
    }

    /// <summary>
    /// The underlying ActivitySource.
    /// </summary>
    public ActivitySource ActivitySource => _activitySource;

    /// <summary>
    /// The trace configuration.
    /// </summary>
    public TraceConfig Config => _config;

    // ── Lambda-based tracing (replaces Python decorators) ───────

    /// <summary>
    /// Executes an operation within a traced span, returning a result.
    /// </summary>
    public T Trace<T>(string name, FISpanKind kind, Func<FISpan, T> operation)
    {
        using var span = StartActiveSpan(name, kind);
        try
        {
            var result = operation(span);
            return result;
        }
        catch (Exception ex)
        {
            span.SetError(ex);
            throw;
        }
    }

    /// <summary>
    /// Executes a void operation within a traced span.
    /// </summary>
    public void Trace(string name, FISpanKind kind, Action<FISpan> operation)
    {
        using var span = StartActiveSpan(name, kind);
        try
        {
            operation(span);
        }
        catch (Exception ex)
        {
            span.SetError(ex);
            throw;
        }
    }

    /// <summary>
    /// Executes an async operation within a traced span, returning a result.
    /// </summary>
    public async Task<T> TraceAsync<T>(string name, FISpanKind kind, Func<FISpan, Task<T>> operation)
    {
        using var span = StartActiveSpan(name, kind);
        try
        {
            var result = await operation(span);
            return result;
        }
        catch (Exception ex)
        {
            span.SetError(ex);
            throw;
        }
    }

    /// <summary>
    /// Executes a void async operation within a traced span.
    /// </summary>
    public async Task TraceAsync(string name, FISpanKind kind, Func<FISpan, Task> operation)
    {
        using var span = StartActiveSpan(name, kind);
        try
        {
            await operation(span);
        }
        catch (Exception ex)
        {
            span.SetError(ex);
            throw;
        }
    }

    // ── Convenience shortcuts ───────────────────────────────────

    public T Chain<T>(string name, Func<FISpan, T> operation) =>
        Trace(name, FISpanKind.Chain, operation);

    public void Chain(string name, Action<FISpan> operation) =>
        Trace(name, FISpanKind.Chain, operation);

    public Task<T> ChainAsync<T>(string name, Func<FISpan, Task<T>> operation) =>
        TraceAsync(name, FISpanKind.Chain, operation);

    public Task ChainAsync(string name, Func<FISpan, Task> operation) =>
        TraceAsync(name, FISpanKind.Chain, operation);

    public T Agent<T>(string name, Func<FISpan, T> operation) =>
        Trace(name, FISpanKind.Agent, operation);

    public void Agent(string name, Action<FISpan> operation) =>
        Trace(name, FISpanKind.Agent, operation);

    public Task<T> AgentAsync<T>(string name, Func<FISpan, Task<T>> operation) =>
        TraceAsync(name, FISpanKind.Agent, operation);

    public Task AgentAsync(string name, Func<FISpan, Task> operation) =>
        TraceAsync(name, FISpanKind.Agent, operation);

    public T Tool<T>(string name, Func<FISpan, T> operation) =>
        Trace(name, FISpanKind.Tool, operation);

    public void Tool(string name, Action<FISpan> operation) =>
        Trace(name, FISpanKind.Tool, operation);

    public Task<T> ToolAsync<T>(string name, Func<FISpan, Task<T>> operation) =>
        TraceAsync(name, FISpanKind.Tool, operation);

    public Task ToolAsync(string name, Func<FISpan, Task> operation) =>
        TraceAsync(name, FISpanKind.Tool, operation);

    public T Llm<T>(string name, Func<FISpan, T> operation) =>
        Trace(name, FISpanKind.Llm, operation);

    public void Llm(string name, Action<FISpan> operation) =>
        Trace(name, FISpanKind.Llm, operation);

    public Task<T> LlmAsync<T>(string name, Func<FISpan, Task<T>> operation) =>
        TraceAsync(name, FISpanKind.Llm, operation);

    public Task LlmAsync(string name, Func<FISpan, Task> operation) =>
        TraceAsync(name, FISpanKind.Llm, operation);

    // ── Manual span creation ────────────────────────────────────

    /// <summary>
    /// Starts a new span (Activity) and returns an FISpan wrapper.
    /// The caller is responsible for disposing the span.
    /// </summary>
    public FISpan StartSpan(string name, FISpanKind kind)
    {
        var activity = _activitySource.StartActivity(name, ActivityKind.Internal)
            ?? throw new InvalidOperationException(
                "Failed to create activity. Ensure a TracerProvider with a listener is configured.");

        activity.SetTag(SemanticConventions.FiSpanKind, kind.ToValue());
        ApplyContextAttributes(activity);

        return new FISpan(activity, _config);
    }

    /// <summary>
    /// Starts a new active span that becomes the current parent for child spans.
    /// Equivalent to StartSpan — in .NET, all Activities are automatically current.
    /// </summary>
    public FISpan StartActiveSpan(string name, FISpanKind kind) =>
        StartSpan(name, kind);

    // ── Attribute helpers ───────────────────────────────────────

    public void SetInputValue(FISpan span, string value) =>
        span.SetInput(value);

    public void SetOutputValue(FISpan span, string value) =>
        span.SetOutput(value);

    public void SetTokenCounts(FISpan span, long inputTokens, long outputTokens, long totalTokens) =>
        span.SetTokenCounts(inputTokens, outputTokens, totalTokens);

    public void SetInputMessages(FISpan span, List<Dictionary<string, string>> messages) =>
        span.SetInputMessages(messages);

    public void SetOutputMessages(FISpan span, List<Dictionary<string, string>> messages) =>
        span.SetOutputMessages(messages);

    public void SetError(FISpan span, Exception error) =>
        span.SetError(error);

    // ── Static message helper (like Java FITracer.message()) ────

    public static Dictionary<string, string> Message(string role, string content) =>
        new() { ["role"] = role, ["content"] = content };

    // ── Internal helpers ────────────────────────────────────────

    private static void ApplyContextAttributes(Activity activity)
    {
        foreach (var (key, value) in ContextAttributes.GetAttributesFromContext())
        {
            activity.SetTag(key, value);
        }
    }
}
