using System.Diagnostics;
using FIInstrumentation.Types;

namespace FIInstrumentation.Tests;

public class FISpanTests : IDisposable
{
    private readonly ActivitySource _source;
    private readonly ActivityListener _listener;
    private readonly List<Activity> _exportedActivities = new();

    public FISpanTests()
    {
        _source = new ActivitySource("test-span-source", "0.1.0");
        _listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name == "test-span-source",
            Sample = (ref ActivityCreationOptions<ActivityContext> _) =>
                ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = activity => _exportedActivities.Add(activity),
        };
        ActivitySource.AddActivityListener(_listener);
    }

    public void Dispose()
    {
        _listener.Dispose();
        _source.Dispose();
    }

    private FISpan CreateSpan(TraceConfig? config = null)
    {
        var activity = _source.StartActivity("test-activity", ActivityKind.Internal)!;
        return new FISpan(activity, config ?? TraceConfig.Builder().Build());
    }

    [Fact]
    public void SetInput_SetsInputValueAndMimeType()
    {
        using var span = CreateSpan();
        span.SetInput("hello world");

        var activity = span.Activity;
        Assert.Equal("hello world", activity.GetTagItem(SemanticConventions.InputValue));
        Assert.Equal("text/plain", activity.GetTagItem(SemanticConventions.InputMimeType));
    }

    [Fact]
    public void SetOutput_SetsOutputValueAndMimeType()
    {
        using var span = CreateSpan();
        span.SetOutput("result", FIMimeType.Json);

        var activity = span.Activity;
        // String values are passed through as-is; mime type is metadata
        Assert.Equal("result", activity.GetTagItem(SemanticConventions.OutputValue));
        Assert.Equal("application/json", activity.GetTagItem(SemanticConventions.OutputMimeType));
    }

    [Fact]
    public void SetInput_WithStringValue_SetsDirectly()
    {
        using var span = CreateSpan();
        span.SetInput("direct string");

        Assert.Equal("direct string", span.Activity.GetTagItem(SemanticConventions.InputValue));
    }

    [Fact]
    public void SetInput_WithObjectValue_SerializesToJson()
    {
        using var span = CreateSpan();
        span.SetInput(new { query = "test" });

        var value = span.Activity.GetTagItem(SemanticConventions.InputValue) as string;
        Assert.NotNull(value);
        Assert.Contains("query", value);
        Assert.Contains("test", value);
    }

    [Fact]
    public void SetTokenCounts_SetsAllThreeAttributes()
    {
        using var span = CreateSpan();
        span.SetTokenCounts(100, 50, 150);

        Assert.Equal(100L, span.Activity.GetTagItem(SemanticConventions.GenAiUsageInputTokens));
        Assert.Equal(50L, span.Activity.GetTagItem(SemanticConventions.GenAiUsageOutputTokens));
        Assert.Equal(150L, span.Activity.GetTagItem(SemanticConventions.GenAiUsageTotalTokens));
    }

    [Fact]
    public void SetTool_SetsToolAttributes()
    {
        using var span = CreateSpan();
        span.SetTool("search", "Searches the web");

        Assert.Equal("search", span.Activity.GetTagItem(SemanticConventions.GenAiToolName));
        Assert.Equal("Searches the web", span.Activity.GetTagItem(SemanticConventions.GenAiToolDescription));
    }

    [Fact]
    public void SetError_SetsStatusAndAttributes()
    {
        using var span = CreateSpan();
        span.SetError(new ArgumentException("bad arg"));

        Assert.Equal(ActivityStatusCode.Error, span.Activity.Status);
        Assert.Equal("System.ArgumentException", span.Activity.GetTagItem(SemanticConventions.ErrorType));
        Assert.Equal("bad arg", span.Activity.GetTagItem(SemanticConventions.ErrorMessage));

        var events = span.Activity.Events.ToList();
        Assert.Single(events);
        Assert.Equal("exception", events[0].Name);
    }

    [Fact]
    public void SetInputMessages_SerializesAsJson()
    {
        using var span = CreateSpan();
        var messages = new List<Dictionary<string, string>>
        {
            new() { ["role"] = "user", ["content"] = "Hello" },
            new() { ["role"] = "assistant", ["content"] = "Hi there" },
        };
        span.SetInputMessages(messages);

        var value = span.Activity.GetTagItem(SemanticConventions.GenAiInputMessages) as string;
        Assert.NotNull(value);
        Assert.Contains("user", value);
        Assert.Contains("Hello", value);
    }

    [Fact]
    public void SetAttribute_String_Works()
    {
        using var span = CreateSpan();
        span.SetAttribute("custom.key", "custom.value");
        Assert.Equal("custom.value", span.Activity.GetTagItem("custom.key"));
    }

    [Fact]
    public void SetAttribute_Long_Works()
    {
        using var span = CreateSpan();
        span.SetAttribute("custom.count", 42L);
        Assert.Equal(42L, span.Activity.GetTagItem("custom.count"));
    }

    [Fact]
    public void SetAttribute_Double_Works()
    {
        using var span = CreateSpan();
        span.SetAttribute("custom.score", 0.95);
        Assert.Equal(0.95, span.Activity.GetTagItem("custom.score"));
    }

    [Fact]
    public void SetAttribute_Bool_Works()
    {
        using var span = CreateSpan();
        span.SetAttribute("custom.flag", true);
        Assert.Equal(true, span.Activity.GetTagItem("custom.flag"));
    }

    [Fact]
    public void Dispose_SetsOkStatus_WhenUnset()
    {
        Activity? captured = null;
        var span = CreateSpan();
        captured = span.Activity;
        span.Dispose();

        Assert.Equal(ActivityStatusCode.Ok, captured.Status);
    }

    [Fact]
    public void Dispose_PreservesErrorStatus()
    {
        Activity? captured = null;
        var span = CreateSpan();
        captured = span.Activity;
        span.SetError(new Exception("fail"));
        span.Dispose();

        Assert.Equal(ActivityStatusCode.Error, captured.Status);
    }

    [Fact]
    public void MaskedInput_ReturnsRedacted_WhenHideInputs()
    {
        var config = TraceConfig.Builder().HideInputs().Build();
        using var span = CreateSpan(config);
        span.SetInput("secret data");

        Assert.Equal(TraceConfig.RedactedValue, span.Activity.GetTagItem(SemanticConventions.InputValue));
    }

    [Fact]
    public void MaskedOutput_ReturnsRedacted_WhenHideOutputs()
    {
        var config = TraceConfig.Builder().HideOutputs().Build();
        using var span = CreateSpan(config);
        span.SetOutput("secret result");

        Assert.Equal(TraceConfig.RedactedValue, span.Activity.GetTagItem(SemanticConventions.OutputValue));
    }

    [Fact]
    public void TraceId_ReturnsValidString()
    {
        using var span = CreateSpan();
        Assert.False(string.IsNullOrEmpty(span.TraceId));
        Assert.Equal(32, span.TraceId.Length); // 128 bits = 32 hex chars
    }

    [Fact]
    public void SpanId_ReturnsValidString()
    {
        using var span = CreateSpan();
        Assert.False(string.IsNullOrEmpty(span.SpanId));
        Assert.Equal(16, span.SpanId.Length); // 64 bits = 16 hex chars
    }
}
