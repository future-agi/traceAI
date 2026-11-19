using System.Diagnostics;

namespace FIInstrumentation.Tests;

/// <summary>
/// Every content key the Python SDK's TraceConfig masks (python/fi_instrumentation/instrumentation/
/// config.py: _INPUT_CONTENT_PREFIXES, _OUTPUT_CONTENT_PREFIXES, _MIXED_CONTENT_PREFIXES and the
/// input/output value keys), plus the alias tool.parameters the C# SDK masks, set through the SDK's own span.
/// </summary>
public class ContentMaskParityTests : IDisposable
{
    private const string Secret = "private-content-DO-NOT-EXPORT";
    private readonly ActivitySource _source = new("content-mask-parity", "0.1.0");
    private readonly ActivityListener _listener;

    public ContentMaskParityTests()
    {
        _listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name == "content-mask-parity",
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(_listener);
    }

    public void Dispose()
    {
        _listener.Dispose();
        _source.Dispose();
    }

    public static readonly string[] InputKeyList =
    [
        "input.value", "raw.input", "llm.prompt_template.variables", "gen_ai.prompt.template.variables",
        "gen_ai.system_instructions", "gen_ai.request.parameters", "gen_ai.request.stop_sequences",
        "gen_ai.agent.description", "gen_ai.image.prompt", "gen_ai.image.negative_prompt",
        "gen_ai.input.images", "gen_ai.prompts", "gen_ai.tool.definitions", "gen_ai.tool.description",
        "gen_ai.tool.call.arguments", "gen_ai.tool.parameters", "tool.parameters",
        "tool_call.function.arguments", "message.function_call_arguments_json",
        "gen_ai.computer_use.text", "gen_ai.computer_use.element_selector", "tool.json_schema",
        "gen_ai.retrieval.query", "gen_ai.reranker.query", "gen_ai.reranker.input_documents",
        "reranker.query", "reranker.input_documents",
    ];

    public static readonly string[] OutputKeyList =
    [
        "output.value", "raw.output", "gen_ai.tool.call.result", "gen_ai.image.revised_prompt",
        "gen_ai.image.output_urls", "gen_ai.guardrail.modified_output", "gen_ai.evaluation.explanation",
        "gen_ai.computer_use.result", "retrieval.documents", "gen_ai.retrieval.documents",
        "gen_ai.reranker.output_documents", "reranker.output_documents",
    ];

    public static readonly string[] EitherKeyList =
    [
        "gen_ai.tool.call", "gen_ai.voice.transcript", "gen_ai.voice.recording.url",
        "gen_ai.voice.recording.stereo_url", "gen_ai.voice.recording.customer_url",
        "gen_ai.voice.recording.assistant_url", "gen_ai.audio.url", "gen_ai.audio.transcript",
        "gen_ai.computer_use.screenshot", "gen_ai.computer_use.current_url",
    ];

    // Identifiers, names and counts: never masked, whatever is hidden.
    public static readonly string[] ControlKeyList =
    [
        "gen_ai.request.model", "gen_ai.tool.name", "gen_ai.tool.call.id", "gen_ai.agent.name",
        "gen_ai.prompt.template.name", "gen_ai.usage.input_tokens", "tool.name", "tool_call.id",
        "gen_ai.retrieval.top_k", "gen_ai.response.id",
        "gen_ai.tool.calls", "input.valued", "tool.parameters_count",
    ];

    public static IEnumerable<object[]> InputKeys => InputKeyList.Select(k => new object[] { k });
    public static IEnumerable<object[]> OutputKeys => OutputKeyList.Select(k => new object[] { k });
    public static IEnumerable<object[]> EitherKeys => EitherKeyList.Select(k => new object[] { k });
    public static IEnumerable<object[]> ControlKeys => ControlKeyList.Select(k => new object[] { k });

    private object? Export(TraceConfig config, string key)
    {
        var activity = _source.StartActivity("parity", ActivityKind.Internal)!;
        using var span = new FISpan(activity, config);
        span.SetAttribute(key, Secret);
        return activity.GetTagItem(key);
    }

    private static TraceConfig Hiding(bool inputs, bool outputs) =>
        TraceConfig.Builder().HideInputs(inputs).HideOutputs(outputs).Build();

    [Theory]
    [MemberData(nameof(InputKeys))]
    public void HideInputs_MasksEveryInputContentKey(string key)
    {
        Assert.Equal(TraceConfig.RedactedValue, Export(Hiding(true, false), key));
        Assert.Equal(Secret, Export(Hiding(false, true), key));
        Assert.Equal(Secret, Export(Hiding(false, false), key));
    }

    [Theory]
    [MemberData(nameof(OutputKeys))]
    public void HideOutputs_MasksEveryOutputContentKey(string key)
    {
        Assert.Equal(TraceConfig.RedactedValue, Export(Hiding(false, true), key));
        Assert.Equal(Secret, Export(Hiding(true, false), key));
        Assert.Equal(Secret, Export(Hiding(false, false), key));
    }

    [Theory]
    [MemberData(nameof(EitherKeys))]
    public void EitherFlag_MasksTheKeysThatCanHoldBothDirections(string key)
    {
        Assert.Equal(TraceConfig.RedactedValue, Export(Hiding(true, false), key));
        Assert.Equal(TraceConfig.RedactedValue, Export(Hiding(false, true), key));
        Assert.Equal(Secret, Export(Hiding(false, false), key));
    }

    [Theory]
    [MemberData(nameof(InputKeys))]
    public void Descendants_OfAContentKeyAreMasked(string key)
    {
        var child = key + ".0.value";
        Assert.Equal(TraceConfig.RedactedValue, Export(Hiding(true, false), child));
    }

    [Theory]
    [MemberData(nameof(ControlKeys))]
    public void ControlKeys_AreNeverMasked(string key)
    {
        var everything = TraceConfig.Builder().HideInputs().HideOutputs().HideInputMessages().HideOutputMessages().Build();
        Assert.Equal(Secret, Export(everything, key));
    }

    [Fact]
    public void MessageKeys_AreRedactedByTheirFlags()
    {
        const string input = "gen_ai.input.messages.0.message.content";
        const string output = "gen_ai.output.messages.0.message.content";
        var inputMessages = TraceConfig.Builder().HideInputMessages().Build();
        var outputMessages = TraceConfig.Builder().HideOutputMessages().Build();
        Assert.Equal(TraceConfig.RedactedValue, Export(inputMessages, input));
        Assert.Equal(Secret, Export(inputMessages, output));
        Assert.Equal(TraceConfig.RedactedValue, Export(outputMessages, output));
        Assert.Equal(Secret, Export(outputMessages, input));
    }

    [Fact]
    public void StringArrays_AreMaskedWithTheSinglePlaceholder()
    {
        var activity = _source.StartActivity("parity", ActivityKind.Internal)!;
        using var span = new FISpan(activity, Hiding(true, false));
        span.SetAttribute("gen_ai.request.stop_sequences", new[] { "END", Secret });
        span.SetAttribute("gen_ai.response.finish_reasons", new[] { "stop" });
        Assert.Equal(TraceConfig.RedactedValue, activity.GetTagItem("gen_ai.request.stop_sequences"));
        Assert.Equal(new[] { "stop" }, activity.GetTagItem("gen_ai.response.finish_reasons"));
    }
}
