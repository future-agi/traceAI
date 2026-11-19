using System.Diagnostics;
using FIInstrumentation.Context;

namespace FIInstrumentation.Tests;

/// <summary>
/// End to end through FITracer / FISpan: every sensitive value written by the module's real
/// setters (input, output, messages, raw input/output, tool parameters, prompt-template
/// variables) must be absent or redacted from every exported tag and event under the hide flags.
/// </summary>
public class ExportedContentUnderHideFlagsTests : IDisposable
{
    private const string Sentinel = "SENTINEL-c7f3-private-content-DO-NOT-EXPORT";
    private const string SourceName = "exported-content-hide-flags";
    private readonly ActivitySource _source = new(SourceName, "0.1.0");
    private readonly ActivityListener _listener;
    private readonly List<Activity> _exported = new();

    public ExportedContentUnderHideFlagsTests()
    {
        _listener = new ActivityListener
        {
            ShouldListenTo = s => s.Name == SourceName,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = a => { lock (_exported) _exported.Add(a); },
        };
        ActivitySource.AddActivityListener(_listener);
    }

    public void Dispose()
    {
        _listener.Dispose();
        _source.Dispose();
    }

    private Activity Run(TraceConfig config)
    {
        _exported.Clear();
        var tracer = new FITracer(_source, config);
        var messages = new List<Dictionary<string, string>> { FITracer.Message("user", Sentinel) };
        using (ContextAttributes.UsingPromptTemplate("tmpl", variables: new() { ["q"] = Sentinel }))
        using (var span = tracer.StartSpan("hide-flags", FISpanKind.Llm))
        {
            span.SetInput(Sentinel);
            span.SetOutput(Sentinel);
            span.SetInputMessages(messages);
            span.SetOutputMessages(messages);
            span.SetTool("tool", Sentinel, new { arg = Sentinel });
            span.SetAttribute("raw.input", Sentinel);
            span.SetAttribute("raw.output", Sentinel);
            span.SetAttribute("gen_ai.tool.call.arguments", Sentinel);
            span.SetAttribute("gen_ai.tool.call.result", Sentinel);
            span.SetError(new InvalidOperationException(Sentinel));
        }
        return Assert.Single(_exported);
    }

    private static IEnumerable<string> Everything(Activity a)
    {
        foreach (var t in a.TagObjects) { yield return t.Key; yield return Convert.ToString(t.Value) ?? ""; }
        yield return a.StatusDescription ?? "";
        foreach (var e in a.Events)
            foreach (var t in e.Tags) { yield return t.Key; yield return Convert.ToString(t.Value) ?? ""; }
    }

    private static void AssertNoSentinelAt(Activity a, params string[] keys)
    {
        foreach (var key in keys)
        {
            var value = a.GetTagItem(key);
            Assert.True(value == null || !Convert.ToString(value)!.Contains(Sentinel), $"{key} leaked the sentinel");
        }
    }

    [Fact]
    public void HideInputs_ExportsNoInputSideSentinel()
    {
        var a = Run(TraceConfig.Builder().HideInputs().Build());
        AssertNoSentinelAt(a, "input.value", "raw.input", "gen_ai.input.messages", "gen_ai.tool.description",
            "gen_ai.tool.parameters", "gen_ai.tool.call.arguments", "gen_ai.prompt.template.variables");
    }

    [Fact]
    public void HideOutputs_ExportsNoOutputSideSentinel()
    {
        var a = Run(TraceConfig.Builder().HideOutputs().Build());
        AssertNoSentinelAt(a, "output.value", "raw.output", "gen_ai.output.messages", "gen_ai.tool.call.result");
    }

    [Fact]
    public void HideInputMessages_HidesInputValueAndMessagesAndTemplateVariables()
    {
        var a = Run(TraceConfig.Builder().HideInputMessages().Build());
        AssertNoSentinelAt(a, "input.value", "raw.input", "gen_ai.input.messages", "gen_ai.prompt.template.variables");
    }

    [Fact]
    public void HideOutputMessages_HidesOutputValueAndMessages()
    {
        var a = Run(TraceConfig.Builder().HideOutputMessages().Build());
        AssertNoSentinelAt(a, "output.value", "raw.output", "gen_ai.output.messages");
    }

    [Fact]
    public void HideInputText_HidesInputValue()
    {
        var a = Run(TraceConfig.Builder().HideInputText().Build());
        AssertNoSentinelAt(a, "input.value", "raw.input", "gen_ai.prompt.template.variables");
    }

    [Fact]
    public void HideOutputText_HidesOutputValue()
    {
        var a = Run(TraceConfig.Builder().HideOutputText().Build());
        AssertNoSentinelAt(a, "output.value", "raw.output");
    }

    [Fact]
    public void AllFlags_ExportNoSentinelAnywhere_IncludingErrorEvents()
    {
        var a = Run(TraceConfig.Builder().HideInputs().HideOutputs().HideInputMessages().HideOutputMessages().Build());
        Assert.DoesNotContain(Everything(a), s => s.Contains(Sentinel));
    }
}
