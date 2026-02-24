using System.Diagnostics;
using FIInstrumentation.Context;

namespace FIInstrumentation.Tests;

public class FITracerTests : IDisposable
{
    private readonly ActivitySource _source;
    private readonly ActivityListener _listener;
    private readonly FITracer _tracer;
    private readonly List<Activity> _exportedActivities = new();

    public FITracerTests()
    {
        _source = new ActivitySource("test-tracer", "0.1.0");
        _tracer = new FITracer(_source, TraceConfig.Builder().Build());

        _listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name == "test-tracer",
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

    [Fact]
    public void Trace_CreatesSpanWithCorrectKind()
    {
        var result = _tracer.Trace("test-span", FISpanKind.Llm, span =>
        {
            Assert.NotNull(span);
            return 42;
        });

        Assert.Equal(42, result);
        Assert.Single(_exportedActivities);
        Assert.Equal("test-span", _exportedActivities[0].DisplayName);
        Assert.Equal("LLM", _exportedActivities[0].GetTagItem(SemanticConventions.FiSpanKind));
    }

    [Fact]
    public void Trace_VoidOverload_Works()
    {
        var executed = false;
        _tracer.Trace("void-span", FISpanKind.Chain, span =>
        {
            executed = true;
        });

        Assert.True(executed);
        Assert.Single(_exportedActivities);
    }

    [Fact]
    public async Task TraceAsync_CreatesSpanAndReturnsResult()
    {
        var result = await _tracer.TraceAsync("async-span", FISpanKind.Agent, async span =>
        {
            await Task.Delay(1);
            return "hello";
        });

        Assert.Equal("hello", result);
        Assert.Single(_exportedActivities);
        Assert.Equal("AGENT", _exportedActivities[0].GetTagItem(SemanticConventions.FiSpanKind));
    }

    [Fact]
    public async Task TraceAsync_VoidOverload_Works()
    {
        var executed = false;
        await _tracer.TraceAsync("async-void", FISpanKind.Tool, async span =>
        {
            await Task.Delay(1);
            executed = true;
        });

        Assert.True(executed);
        Assert.Single(_exportedActivities);
    }

    [Fact]
    public void Chain_IsShortcutForChainKind()
    {
        _tracer.Chain("my-chain", span =>
        {
            span.SetInput("input");
        });

        Assert.Single(_exportedActivities);
        Assert.Equal("CHAIN", _exportedActivities[0].GetTagItem(SemanticConventions.FiSpanKind));
    }

    [Fact]
    public void Agent_IsShortcutForAgentKind()
    {
        var result = _tracer.Agent("my-agent", span => "result");

        Assert.Equal("result", result);
        Assert.Single(_exportedActivities);
        Assert.Equal("AGENT", _exportedActivities[0].GetTagItem(SemanticConventions.FiSpanKind));
    }

    [Fact]
    public void Tool_IsShortcutForToolKind()
    {
        var result = _tracer.Tool("my-tool", span => 123);

        Assert.Equal(123, result);
        Assert.Single(_exportedActivities);
        Assert.Equal("TOOL", _exportedActivities[0].GetTagItem(SemanticConventions.FiSpanKind));
    }

    [Fact]
    public void Llm_IsShortcutForLlmKind()
    {
        _tracer.Llm("my-llm", span =>
        {
            span.SetAttribute(SemanticConventions.GenAiRequestModel, "gpt-4");
        });

        Assert.Single(_exportedActivities);
        Assert.Equal("LLM", _exportedActivities[0].GetTagItem(SemanticConventions.FiSpanKind));
        Assert.Equal("gpt-4", _exportedActivities[0].GetTagItem(SemanticConventions.GenAiRequestModel));
    }

    [Fact]
    public void Trace_SetsErrorOnException()
    {
        Assert.Throws<InvalidOperationException>(() =>
            _tracer.Trace<int>("error-span", FISpanKind.Chain, span =>
            {
                throw new InvalidOperationException("test error");
            }));

        Assert.Single(_exportedActivities);
        Assert.Equal(ActivityStatusCode.Error, _exportedActivities[0].Status);
        Assert.Equal("System.InvalidOperationException",
            _exportedActivities[0].GetTagItem(SemanticConventions.ErrorType));
        Assert.Equal("test error",
            _exportedActivities[0].GetTagItem(SemanticConventions.ErrorMessage));
    }

    [Fact]
    public void StartSpan_ReturnsManualSpan()
    {
        using var span = _tracer.StartSpan("manual-span", FISpanKind.Embedding);
        span.SetAttribute(SemanticConventions.EmbeddingModelName, "text-embedding-ada-002");
        span.SetTokenCounts(100, 0, 100);
    }

    [Fact]
    public void Trace_CreatesNestedSpans()
    {
        _tracer.Chain("parent", parentSpan =>
        {
            _tracer.Tool("child", childSpan =>
            {
                childSpan.SetInput("tool-input");
            });
        });

        Assert.Equal(2, _exportedActivities.Count);
    }

    [Fact]
    public void Message_CreatesCorrectDictionary()
    {
        var msg = FITracer.Message("user", "Hello!");
        Assert.Equal("user", msg["role"]);
        Assert.Equal("Hello!", msg["content"]);
    }

    [Fact]
    public void ContextAttributes_AreAppliedToSpans()
    {
        using (ContextAttributes.UsingSession("session-123"))
        using (ContextAttributes.UsingUser("user-456"))
        {
            _tracer.Chain("with-context", span => { });
        }

        Assert.Single(_exportedActivities);
        Assert.Equal("session-123", _exportedActivities[0].GetTagItem(SemanticConventions.SessionId));
        Assert.Equal("user-456", _exportedActivities[0].GetTagItem(SemanticConventions.UserId));
    }
}
