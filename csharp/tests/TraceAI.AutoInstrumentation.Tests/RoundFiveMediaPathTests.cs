using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFiveMediaPathTests : InitIsolation
{
    private const string FlatAudio="{\"parts.1.inlineData.mimeType\":\"audio/wav\",\"parts.1.inlineData.data\":\"QUJDREVG\",\"parts.1.inlineData.transcript\":\"spoken words\"}";
    private static AutoConfig Config()=>new(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });

    [Theory]
    [InlineData("parts.1.inlineData.mimeType","parts.1.inlineData.data","audio/wav")]
    [InlineData("parts.1.inlineData.mime_type","parts.1.inlineData.bytes.0","image/png")]
    [InlineData("mimeType","data.0.1","image/png")]
    [InlineData("mime_type","bytes.0","image/png")]
    [InlineData("parts.1.inlineData.MIME_TYPE","parts.1.inlineData.DATA.0","audio/wav")]
    [InlineData("parts.1.inlineData.mimeType.0","parts.1.inlineData.data.0","image/png")]
    public void JsonMediaPayloadPathsUseSiblingMimeIncludingIndexedDescendants(string mimeKey,string dataKey,string mime)
    {
        // Data precedes MIME deliberately: neither key order nor payload size matters.
        var original=new JsonObject { [dataKey]="QUJDREVG",[mimeKey]=mime,["transcript"]="spoken words",
            ["parts.10.inlineData.data"]="ordinary data",["parts.1.inlineData.database"]="ordinary database" };
        var safe=JsonNode.Parse(WirePrivacy.ScrubString(original.ToJsonString()))!;
        Assert.Equal("<omitted: 8 base64 chars>",safe[dataKey]!.GetValue<string>());
        Assert.Equal(mime,safe[mimeKey]!.GetValue<string>());
        Assert.Equal("spoken words",safe["transcript"]!.GetValue<string>());
        // A root MIME hint applies throughout this root object; indexed hints do
        // not bleed into another array element. Both scopes follow the S6 catalog.
        Assert.Equal(mimeKey is "mimeType" or "mime_type"?"<omitted: media>":"ordinary data",safe["parts.10.inlineData.data"]!.GetValue<string>());
        Assert.Equal("ordinary database",safe["parts.1.inlineData.database"]!.GetValue<string>());
        Assert.Equal("QUJDREVG",original[dataKey]!.GetValue<string>());
    }

    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task FlattenedMimeAndIndexedPayloadsAreRemovedOnEveryActualOtlpSurface(bool compatibility)
    {
        var tags=new ActivityTagsCollection {
            ["output.value"]=FlatAudio,
            ["input.parts.0.inlineData.data.0"]="R0hJSktM",
            ["input.parts.0.inlineData.bytes.0.1"]=new[] { "MQ==","Mg==" },
            ["input.parts.0.inlineData.mimeType"]="image/png",
            ["input.parts.0.inlineData.format"]="png",
            ["input.parts.10.inlineData.data.0"]="ordinary data",
            ["input.parts.0.inlineData.database.0"]="ordinary database",
            ["json.array"]="[{\"inlineData\":{\"bytes.0\":\"Mw==\",\"mime_type\":\"audio/wav\",\"transcript\":\"spoken words\"}}]" };
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config());
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("Round5.MediaPaths",FlatAudio,tags:tags);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(tags.Select(pair=>new KeyValuePair<string,object>(pair.Key,pair.Value!))))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using var document=JsonDocument.Parse(FlatAudio);
        using var original=source.StartActivity("media paths",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),tags) })!;
        foreach(var pair in tags) original.SetTag(pair.Key,pair.Value);
        original.SetTag("json.element",document.RootElement); original.SetTag("json.node",JsonNode.Parse(FlatAudio));
        original.DisplayName=FlatAudio; original.SetStatus(ActivityStatusCode.Error,FlatAudio);
        original.AddEvent(new ActivityEvent(FlatAudio,tags:tags)); original.Stop();
        byte[] bytes=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(bytes);
        foreach(string payload in new[] { "QUJDREVG","R0hJSktM","MQ==","Mg==","Mw==" }) Assert.DoesNotContain(payload,actual);
        var safe=Assert.Single(Otlp.Spans(bytes));
        Assert.Equal("<omitted: 8 base64 chars>",safe.Attributes["input.parts.0.inlineData.data.0"]);
        Assert.Equal(new object[] { "<omitted: 4 base64 chars>","<omitted: 4 base64 chars>" },Assert.IsType<object[]>(safe.Attributes["input.parts.0.inlineData.bytes.0.1"]));
        Assert.Contains("spoken words",actual); Assert.Contains("ordinary data",actual); Assert.Contains("ordinary database",actual);
        Assert.Equal("png",safe.Attributes["input.parts.0.inlineData.format"]);
        Assert.Equal("R0hJSktM",original.GetTagItem("input.parts.0.inlineData.data.0"));
        Assert.Equal(FlatAudio,original.StatusDescription); Assert.Equal(FlatAudio,source.Version);
    }

    [Theory]
    [InlineData("net8.0","flat")] [InlineData("net10.0","flat")]
    [InlineData("net8.0","indexed")] [InlineData("net10.0","indexed")]
    public async Task RealHookRemovesFlattenedMimeAndIndexedPayloadsWithBaselineParity(string framework,string shape)
    {
        await using var server=new MockServer([],"application/json");
        var environment=new Dictionary<string,string?> { ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
        string mode="round5_media_paths_"+shape;
        var baseline=await StartupHookTests.RunApp(server.Url,mode,new(environment) { ["DOTNET_STARTUP_HOOKS"]=null },framework);
        var traced=await StartupHookTests.RunApp(server.Url,mode,environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        Assert.Equal("media-paths-complete",traced.Output.Trim());
        byte[] bytes=Assert.Single(server.Exports); var span=Assert.Single(Otlp.Spans(bytes));
        string actual=Encoding.UTF8.GetString(bytes);
        Assert.DoesNotContain("QUJDREVG",actual); Assert.DoesNotContain("R0hJSktM",actual);
        Assert.Contains("spoken words",actual);
        if(shape=="indexed") Assert.Equal("<omitted: 8 base64 chars>",span.Attributes["input.parts.0.inlineData.data.0"]);
    }
}
