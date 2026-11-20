// Engineering-only contract test: real published SDK + stock exporter, no provider wrapper.
using System.Collections.Concurrent;
using System.Diagnostics;
using Anthropic;
using Anthropic.Core;
using Anthropic.Models.Messages;
using Anthropic.Helpers.Beta;
using Beta = Anthropic.Models.Beta.Messages;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

string scenario = args[0];
Uri api = new(args[1]);
Uri otlp = new(args[2]);
if (!api.IsLoopback || !otlp.IsLoopback)
    throw new ArgumentException("Contract tests only accept loopback endpoints.");

Environment.SetEnvironmentVariable("ANTHROPIC_OPEN_TELEMETRY", null);
Environment.SetEnvironmentVariable("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", null);
Environment.SetEnvironmentVariable("ANTHROPIC_OPEN_TELEMETRY_TRACES_MAX_CONTENT_BYTES", null);
if (scenario == "disabled-env")
    Environment.SetEnvironmentVariable("ANTHROPIC_OPEN_TELEMETRY", "false");
if (scenario == "environment-content" || scenario == "option-precedence")
    Environment.SetEnvironmentVariable("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content");
if (scenario == "option-enables")
    Environment.SetEnvironmentVariable("ANTHROPIC_OPEN_TELEMETRY", "false");

var ended = new ConcurrentBag<Activity>();
using var provider = Sdk.CreateTracerProviderBuilder()
    .SetResourceBuilder(ResourceBuilder.CreateDefault().AddService("anthropic-csharp-contract")
        .AddAttributes(new Dictionary<string, object>
        {
            ["project_name"] = "anthropic-csharp-contract",
            ["project_type"] = "observe",
        }))
    .AddSource("native-contract", scenario == "unsubscribed" ? "fi-instrumentation" : "com.anthropic.sdk.csharp*")
    .AddProcessor(new EndedActivities(ended))
    .AddOtlpExporter(o =>
    {
        o.Endpoint = otlp;
        o.Protocol = OtlpExportProtocol.HttpProtobuf;
        o.Headers = "X-Api-Key=synthetic-fi-api,X-Secret-Key=synthetic-fi-secret";
        o.TimeoutMilliseconds = 5000;
        o.ExportProcessorType = ExportProcessorType.Batch;
        o.BatchExportProcessorOptions = new() { ScheduledDelayMilliseconds = 60000, ExporterTimeoutMilliseconds = 5000 };
    }).Build();
using var source = new ActivitySource("native-contract");
IAnthropicClient client = new AnthropicClient { ApiKey = "synthetic-anthropic-key", BaseUrl = api.ToString(), MaxRetries = 1 };
#if !BEFORE_NATIVE
if (scenario is "content" or "stream" or "early-break" or "early-dispose" or "cancel" or "stream-error" or "truncated-stream" or "truncation" or "tool-content" or "binary-content")
    client = client.WithOptions(o => o with
    {
        OpenTelemetry = new() { Traces = new() { ContentMode = OpenTelemetryContentMode.Content,
            MaxContentBytes = scenario == "truncation" ? 128 : 750000 } },
    });
if (scenario is "option-precedence" or "option-enables")
    client = client.WithOptions(o => o with { OpenTelemetry = new() });
if (scenario == "disabled-option")
    client = client.WithOptions(o => o with { OpenTelemetry = false });
#endif

List<ContentBlockParam> Content()
{
    List<ContentBlockParam> blocks = [new TextBlockParam { Text = scenario == "truncation" ? new string('x', 2000) + "PROMPT_SENTINEL" : "PROMPT_SENTINEL" }];
    if (scenario.StartsWith("binary-"))
        blocks.Add(new ImageBlockParam { Source = new Base64ImageSource { MediaType = "image/png", Data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6D8AAAAASUVORK5CYII=" } });
    return blocks;
}

MessageCreateParams Parameters() => new()
{
    Model = "claude-opus-5-5",
    MaxTokens = 256,
    System = "SYSTEM_SENTINEL",
#if LEGACY_MESSAGES
    Messages = [new MessageParam { Role = Role.User, Content = Content() }],
#else
    Messages = [new UserMessageParam { Content = Content() }],
#endif
};

if (scenario == "concurrent")
{
#if !BEFORE_NATIVE
    async Task Call(string id)
    {
        using var parent = source.StartActivity(id);
        var scoped = client.WithOptions(o => o with { OpenTelemetryRequest = new() { ConversationId = id } });
        await scoped.Messages.Create(Parameters());
        if (Activity.Current != parent) throw new Exception("Parent context leaked");
    }
    await Task.WhenAll(Call("conversation-a"), Call("conversation-b"));
#endif
}
else
{
    using var parent = source.StartActivity("application-request");
    if (scenario is "stream" or "early-break" or "early-dispose" or "cancel" or "stream-error" or "truncated-stream")
    {
        using var cts = new CancellationTokenSource();
        var stream = client.Messages.CreateStreaming(Parameters(), cancellationToken: cts.Token);
        if (scenario == "early-dispose")
        {
            await using var it = stream.GetAsyncEnumerator();
            if (!await it.MoveNextAsync()) throw new Exception("Missing first stream event");
            if (ended.Any(a => a.Source.Name.StartsWith("com.anthropic"))) throw new Exception("Stream ended before disposal");
        }
        else
        {
            bool sawFailure = false;
            try
            {
                await foreach (var item in stream)
                {
                    if (ended.Any(a => a.Source.Name.StartsWith("com.anthropic"))) throw new Exception("Stream ended before exhaustion");
                    if (scenario == "early-break") break;
                    if (scenario == "cancel") cts.Cancel();
                }
            }
            catch (OperationCanceledException) when (scenario == "cancel") { sawFailure = true; }
            catch (Anthropic.Exceptions.AnthropicSseException) when (scenario == "stream-error") { sawFailure = true; }
            if ((scenario is "cancel" or "stream-error") && !sawFailure) throw new Exception("Expected failure not received");
        }
    }
    else if (scenario is "tool-content" or "tool-metadata")
    {
        int calls = 0;
        var tool = new BetaRunnableTool
        {
            Name = "lookup",
            Definition = new Beta.BetaTool { Name = "lookup", InputSchema = new() { Properties = new Dictionary<string, System.Text.Json.JsonElement>() } },
            Run = (_, _) => { calls++; return Task.FromResult<Beta.BetaToolResultBlockParamContent>("TOOL_RESULT_SENTINEL"); },
        };
        var runner = client.Beta.Messages.ToolRunner(new Beta.MessageCreateParams
        {
            Model = "claude-opus-5-5", MaxTokens = 256,
#if LEGACY_MESSAGES
            Messages = [new Beta.BetaMessageParam { Role = Beta.Role.User, Content = "PROMPT_SENTINEL" }],
#else
            Messages = [new Beta.BetaUserMessageParam { Content = "PROMPT_SENTINEL" }],
#endif
        }, [tool], maxIterations: 3);
        int messages = 0;
        await foreach (var _ in runner) messages++;
        if (calls != 1 || messages != 2) throw new Exception("Tool loop did not complete exactly once");
    }
    else if (scenario == "http-error")
    {
        try { await client.Messages.Create(Parameters()); throw new Exception("Expected API failure"); }
        catch (Anthropic.Exceptions.AnthropicBadRequestException) { }
    }
    else
    {
        var response = await client.Messages.Create(Parameters());
        if (response.ID != "msg_contract") throw new Exception("Unexpected fixture response");
    }
    if (Activity.Current != parent) throw new Exception("Parent context leaked");
}

if (!provider.ForceFlush(5000)) throw new Exception("Batch flush timed out");
#if !BEFORE_NATIVE
int expected = scenario is "unsubscribed" or "disabled-env" or "disabled-option" ? 0 : scenario == "concurrent" ? 2 : scenario.StartsWith("tool-") ? 4 : 1;
if (ended.Count(a => a.Source.Name.StartsWith("com.anthropic")) != expected)
    throw new Exception("Unexpected number of completed native activities");
#endif
Console.WriteLine($"Completed {scenario}");

sealed class EndedActivities(ConcurrentBag<Activity> activities) : BaseProcessor<Activity>
{
    public override void OnEnd(Activity activity) => activities.Add(activity);
}
