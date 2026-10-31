using System.Diagnostics;
using System.Text;
using System.Text.Json;
using OpenTelemetry;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundTwoRegressionTests : InitIsolation
{
    private static AutoOptions Options(BaseExporter<Activity>? exporter=null) => new() {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","no-such-config.toml") };
    private sealed class Rendered(string text) { internal int Calls; public override string ToString() { Calls++; return text; } }
    private sealed class Throwing { internal int Calls; public override string ToString() { Calls++; throw new InvalidOperationException(); } }
    private sealed class Enrich(Action<Activity> action) : BaseProcessor<Activity>
    { public override void OnEnd(Activity activity)=>action(activity); }
    public static IEnumerable<object[]> LateTypes => from kind in new[] { "element","object","array","throwing" }
        from compatibility in new[] { false,true } select new object[] { kind,compatibility };
    [Theory]
    [MemberData(nameof(LateTypes))]
    public async Task LateUnsupportedAttributesAreScrubbedBeforeActualOtlpSerialization(string kind,bool compatibility)
    {
        await using var collector=new MockServer([],"application/json");
        var options=Options();
        var privacy=new AutoProcessor(new AutoConfig(options));
        string media=new('A',512);
        string payload=JsonSerializer.Serialize(new { api_key="PRIVATE_JSON_KEY",echo=options.ApiKey,data=media });
        using var document=JsonDocument.Parse(payload);
        object value=kind switch { "element"=>document.RootElement,"object"=>new Rendered(payload),"array"=>new object[] { document.RootElement,new Rendered(payload) },_=>new Throwing() };
        var exporter=new TrackingOtlpExporter(new OpenTelemetry.Exporter.OtlpExporterOptions {
            Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),forceCompatibility:compatibility);
        // A synchronous processor chain deterministically adds the object AFTER the
        // first privacy pass and BEFORE the real OTLP export. No timer/batch races.
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(privacy).AddProcessor(new Enrich(a=>a.SetTag("late",value)))
            .AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var activity=TraceAIAuto.Source.StartActivity("wire",ActivityKind.Client,default(ActivityContext),
            new ActivityTagsCollection { ["traceai.capture.layer"]="wire" })) Assert.NotNull(activity);
        var span=Assert.Single(collector.Exports.SelectMany(Otlp.Spans));
        string actual=WireCore.J(span.Attributes);
        Assert.DoesNotContain("PRIVATE_JSON_KEY",actual); Assert.DoesNotContain(options.ApiKey!,actual); Assert.DoesNotContain(media,actual);
        if(kind is "element" or "array") { Assert.Contains("<redacted>",actual); Assert.Contains("<omitted: 512 base64 chars>",actual); }
        else Assert.Contains(value.GetType().FullName!,actual);
        if(value is Rendered rendered) Assert.Equal(0,rendered.Calls);
        if(value is Throwing throwing) Assert.Equal(0,throwing.Calls);
    }
    [Fact]
    public void CyclicAttributeArraysAreBoundedAndFailClosed()
    {
        object[] cyclic=new object[1]; cyclic[0]=cyclic;
        object? safe=WirePrivacy.Scrub(cyclic);
        for(int depth=0;safe is object[] nested;depth++) { Assert.True(depth<64); safe=Assert.Single(nested); }
        Assert.Equal("<redacted>",safe);
    }
    [Theory]
    [InlineData("x-api-key",false)] [InlineData("api-key",false)] [InlineData("xi-api-key",false)]
    [InlineData("x-goog-api-key",false)] [InlineData("anthropic-api-key",false)] [InlineData("Ocp-Apim-Subscription-Key",false)]
    [InlineData("x-api-key",true)]
    public async Task EachCredentialHeaderValueIsRemovedWhenEchoedSeparately(string header,bool contentHeader)
    {
        const string first="FIRST_PRIVATE_KEY",second="SECOND_PRIVATE_KEY";
        await using var server=new MockServer(Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"bad "+first+" and "+second+"\"}}"),"application/json") { StatusCode=401 };
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") {
            Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        if(contentHeader) request.Content.Headers.TryAddWithoutValidation(header,new[] { first,second });
        else request.Headers.TryAddWithoutValidation(header,new[] { first,second });
        using var response=await client.SendAsync(request); await response.Content.ReadAsStringAsync();
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal("bad <redacted> and <redacted>",span.StatusDescription);
        Assert.Contains(first,Assert.Single(server.RequestHeaders)[header]);
        Assert.Contains(second,Assert.Single(server.RequestHeaders)[header]);
    }
    [Fact]
    public async Task ConfiguredCredentialsCannotLeakThroughResourceFields()
    {
        await using var collector=new MockServer([],"application/json");
        var options=Options(); options.BaseUrl=collector.Url;
        options.ProjectName="project-"+options.ApiKey; options.ProjectVersionId=options.SecretKey;
        options.ProjectVersionName="version-"+options.ApiKey; options.Metadata=new() { ["value"]=options.SecretKey! };
        var service=Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME");
        try
        {
            Environment.SetEnvironmentVariable("OTEL_SERVICE_NAME","service-"+options.ApiKey);
            using var agent=new Agent(new AutoConfig(options));
            using(var activity=TraceAIAuto.Source.StartActivity("resource")) Assert.NotNull(activity);
            Assert.True(agent.Flush(3000)); var span=Assert.Single(collector.Exports.SelectMany(Otlp.Spans));
            Assert.Equal("service-<redacted>",span.Resource["service.name"]);
            Assert.Equal("project-<redacted>",span.Resource["project_name"]);
            Assert.Equal("<redacted>",span.Resource["project_version_id"]);
            Assert.DoesNotContain(options.ApiKey!,WireCore.J(span.Resource)); Assert.DoesNotContain(options.SecretKey!,WireCore.J(span.Resource));
        }
        finally { Environment.SetEnvironmentVariable("OTEL_SERVICE_NAME",service); }
    }
    private sealed class BlockedExporter : BaseExporter<Activity>
    {
        internal readonly ManualResetEventSlim Entered=new(),Release=new(),Finished=new();
        public override ExportResult Export(in Batch<Activity> batch)
        {
            Entered.Set(); Release.Wait(); Finished.Set(); return ExportResult.Success;
        }
    }
    [Fact]
    public void PublicFlushCannotSucceedUntilBlockedExportActuallyReturns()
    {
        var exporter=new BlockedExporter();
        try
        {
            TraceAIAuto.Init(o=> { o.ApiKey="explicit-api-key"; o.SecretKey="explicit-secret-key"; o.Exporter=exporter; });
            for(int i=0;i<512;i++) using(var activity=TraceAIAuto.Source.StartActivity("blocked")) Assert.NotNull(activity);
            Assert.True(exporter.Entered.Wait(5000));
            Assert.False(TraceAIAuto.Flush(50)); Assert.False(exporter.Finished.IsSet);
            exporter.Release.Set();
            Assert.True(TraceAIAuto.Flush(3000)); Assert.True(exporter.Finished.IsSet);
        }
        finally { exporter.Release.Set(); TraceAIAuto.Shutdown(); }
    }
    [Theory]
    [InlineData(false,false)] [InlineData(false,true)] [InlineData(true,false)] [InlineData(true,true)]
    public async Task ExactContentLengthConsumptionDoesNotRequireAnEofRead(bool binary,bool asyncDispose)
    {
        byte[] bytes=binary?new byte[63]:"{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"complete\"}}]}"u8.ToArray();
        await using var server=new MockServer(bytes,binary?"audio/mpeg":"application/json") { UseContentLength=true };
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+(binary?"v1/audio/speech":"v1/chat/completions")) {
            Content=new StringContent(binary?"{\"input\":\"hi\"}":"{\"messages\":[]}",Encoding.UTF8,"application/json") };
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
        Assert.Equal(bytes.Length,response.Content.Headers.ContentLength);
        var body=await response.Content.ReadAsStreamAsync(); var buffer=new byte[bytes.Length];
        await body.ReadExactlyAsync(buffer); Assert.Equal(bytes,buffer);
        if(asyncDispose) await body.DisposeAsync(); else { response.Dispose(); body.Dispose(); }
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Null(span.GetTagItem("traceai.wire.incomplete"));
        if(binary) Assert.Equal((long)bytes.Length,span.GetTagItem("traceai.audio.output_bytes"));
        else Assert.Equal("complete",span.GetTagItem("output.value"));
    }
    [Theory]
    [InlineData(true)] [InlineData(false)]
    public void GeminiJsonArrayStreamRecordsStreamFlagAndMeasuredFirstChunk(bool streaming)
    {
        string url="https://generativelanguage.googleapis.com/v1beta/models/m:"+(streaming?"streamGenerateContent":"generateContent");
        var request=new RequestData { Url=url,Body="{\"contents\":[]}"u8.ToArray() };
        var response=new ResponseData { Status=200,FirstChunkSeconds=0.123456,
            Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-type"]="application/json" },
            Body="[{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"one\"}]}}]},{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"two\"}]}}]}]"u8.ToArray() };
        var result=WireCore.Build(WireCore.Match("POST",url)!,request,response); Assert.NotNull(result);
        Assert.Equal("onetwo",result.Attributes["output.value"]);
        if(streaming) { Assert.Equal(true,result.Attributes["gen_ai.request.stream"]); Assert.Equal(0.123456,result.Attributes["gen_ai.response.time_to_first_chunk"]); }
        else { Assert.False(result.Attributes.ContainsKey("gen_ai.request.stream")); Assert.False(result.Attributes.ContainsKey("gen_ai.response.time_to_first_chunk")); }
    }
}
