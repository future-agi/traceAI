using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Trace;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>
/// Content keys written on application spans must not reach the actual OTLP request body under the
/// hide flags that Python (and the FIInstrumentation TraceConfig) apply to them, including the
/// message and text flags that must also hide the opaque input.value / output.value duplicates.
/// Every value carries a per-key sentinel so a leak names the key.
/// </summary>
public class ContentKeysUnderHideFlagsTests : InitIsolation
{
    private static readonly string[] InputKeys =
    [
        "input.value", "raw.input", "gen_ai.prompt.template.variables", "llm.prompt_template.variables",
        "gen_ai.input.messages", "gen_ai.input.messages.0.message.content",
        "gen_ai.input.messages.0.message.contents.0.message_content.text",
        "gen_ai.system_instructions", "gen_ai.tool.call.arguments", "tool_call.function.arguments",
        "tool.parameters", "gen_ai.tool.parameters", "gen_ai.tool.definitions",
        "embedding.embeddings.0.embedding.text", "gen_ai.retrieval.query", "gen_ai.reranker.query",
    ];

    private static readonly string[] OutputKeys =
    [
        "output.value", "raw.output", "gen_ai.output.messages", "gen_ai.output.messages.0.message.content",
        "gen_ai.output.messages.0.message.contents.0.message_content.text", "gen_ai.tool.call.result",
        "retrieval.documents.0.document.content", "gen_ai.retrieval.documents",
    ];

    private static string[] ValueKeysFor(string flag) => flag switch
    {
        "inputs" or "input_messages" or "input_text" => ["input.value", "raw.input"],
        _ => ["output.value", "raw.output"],
    };

    private static TraceConfig Config(string flag) => flag switch
    {
        "inputs" => new TraceConfig { HideInputs = true },
        "outputs" => new TraceConfig { HideOutputs = true },
        "input_messages" => new TraceConfig { HideInputMessages = true },
        "output_messages" => new TraceConfig { HideOutputMessages = true },
        "input_text" => new TraceConfig { HideInputText = true },
        "output_text" => new TraceConfig { HideOutputText = true },
        _ => throw new ArgumentOutOfRangeException(nameof(flag)),
    };

    private static async Task<string> Export(TraceConfig mask, IEnumerable<string> keys)
    {
        await using var collector = new MockServer([], "application/json");
        var config = new AutoConfig(new AutoOptions
        {
            ApiKey = "explicit-api-key", SecretKey = "explicit-secret-key", TraceConfig = mask,
            ConfigFile = Path.Combine(WireFixtureTests.Repo, "csharp", "absent.toml"),
        });
        var privacy = new AutoProcessor(config);
        using var exporter = new TrackingOtlpExporter(new OtlpExporterOptions
        {
            Endpoint = new Uri(collector.Url + "tracer/v1/traces"), Protocol = OtlpExportProtocol.HttpProtobuf,
        }, privacy, new ExportCompletion(), false);
        using var source = new ActivitySource("Content.UnderHide");
        using var provider = Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using (var span = source.StartActivity("content", ActivityKind.Client))
        {
            Assert.NotNull(span);
            foreach (var key in keys) span.SetTag(key, "SENTINEL:" + key + ":END");
            span.SetTag("gen_ai.request.model", "model-name-kept");
        }
        return Encoding.UTF8.GetString(Assert.Single(collector.Exports));
    }

    private static void AssertHidden(string body, IEnumerable<string> keys, string flag)
    {
        foreach (var key in keys)
            Assert.False(body.Contains("SENTINEL:" + key + ":END", StringComparison.Ordinal), $"{key} leaked under {flag}");
    }

    [Theory]
    [InlineData("inputs")]
    [InlineData("input_messages")]
    [InlineData("input_text")]
    public async Task InputFlags_HideTheOpaqueInputValueKeys(string flag)
    {
        string body = await Export(Config(flag), InputKeys.Concat(OutputKeys));
        AssertHidden(body, ValueKeysFor(flag), flag);
        Assert.Contains("model-name-kept", body);
    }

    [Theory]
    [InlineData("outputs")]
    [InlineData("output_messages")]
    [InlineData("output_text")]
    public async Task OutputFlags_HideTheOpaqueOutputValueKeys(string flag)
    {
        string body = await Export(Config(flag), InputKeys.Concat(OutputKeys));
        AssertHidden(body, ValueKeysFor(flag), flag);
        Assert.Contains("model-name-kept", body);
    }

    [Theory]
    [InlineData("input_messages")]
    [InlineData("input_text")]
    public async Task InputMessageAndTextFlags_HideInputMessageContent(string flag)
    {
        string body = await Export(Config(flag), InputKeys);
        AssertHidden(body, InputKeys.Where(k => k.StartsWith("gen_ai.input.messages", StringComparison.Ordinal)), flag);
    }

    [Theory]
    [InlineData("output_messages")]
    [InlineData("output_text")]
    public async Task OutputMessageAndTextFlags_HideOutputMessageContent(string flag)
    {
        string body = await Export(Config(flag), OutputKeys);
        AssertHidden(body, OutputKeys.Where(k => k.StartsWith("gen_ai.output.messages", StringComparison.Ordinal)), flag);
    }

    [Fact]
    public async Task HideInputs_HidesEveryInputContentKey()
    {
        string body = await Export(Config("inputs"), InputKeys);
        AssertHidden(body, InputKeys, "inputs");
    }

    [Fact]
    public async Task HideOutputs_HidesEveryOutputContentKey()
    {
        string body = await Export(Config("outputs"), OutputKeys);
        AssertHidden(body, OutputKeys, "outputs");
    }
}
