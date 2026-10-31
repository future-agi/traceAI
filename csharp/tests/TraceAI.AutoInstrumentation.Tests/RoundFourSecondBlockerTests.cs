using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFourSecondBlockerTests : InitIsolation
{
    private static AutoConfig Config(TraceConfig? mask=null,BaseExporter<Activity>? exporter=null)=>new(new AutoOptions {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",TraceConfig=mask,Exporter=exporter,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    private static Dictionary<string,string?> EnvironmentFor(MockServer server)=>new() {
        ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task TextHideFlagsCoverDotTextFamiliesOnEveryActualOtlpSurface(bool compatibility)
    {
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config(new TraceConfig { HideInputText=true,HideOutputText=true }));
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions {
            Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        string[] keys=["input.text","output.text","gen_ai.input.messages.0.parts.0.text","gen_ai.output.messages.0.parts.0.text"];
        var tags=new ActivityTagsCollection(keys.Select(key=>new KeyValuePair<string,object?>(key,new[] { "PRIVATE_DOT_TEXT" })));
        using var source=new ActivitySource("Round4.DotText",tags:tags);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(tags.Select(pair=>new KeyValuePair<string,object>(pair.Key,pair.Value!))))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=source.StartActivity("text families",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),tags) }))
        {
            Assert.NotNull(span);
            foreach(var pair in tags) span.SetTag(pair.Key,pair.Value);
            span.SetTag("gen_ai.input.messages.0.role","user");
            span.AddEvent(new ActivityEvent("event",tags:tags));
        }
        byte[] bytes=Assert.Single(collector.Exports); Assert.DoesNotContain("PRIVATE_DOT_TEXT",Encoding.UTF8.GetString(bytes));
        var safe=Assert.Single(Otlp.Spans(bytes));
        foreach(string key in keys) { Assert.Equal(TraceConfig.RedactedValue,safe.Attributes[key]); Assert.Equal(TraceConfig.RedactedValue,safe.Resource[key]); }
        Assert.Equal("user",safe.Attributes["gen_ai.input.messages.0.role"]);
        Assert.NotEmpty(Otlp.Extras(bytes,11)); Assert.NotEmpty(Otlp.Extras(bytes,13));
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task PercentEscapedDataUrlsCannotLeaveEncodedPayloadInActualOtlp(bool compatibility)
    {
        const string escaped="%51%55%4A%44%52%45%56%47%53%41%3D%3D";
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config());
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions {
            Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=TraceAIAuto.Source.StartActivity("escaped media"))
        {
            span!.SetTag("media","prefix data:image/png;base64,"+escaped+" suffix");
            span.SetTag("input.value","{\"image_url\":\"data:image/png;base64,QUJD%52%45VGSA%3D%3D\"}");
            span.SetStatus(ActivityStatusCode.Error,"data:audio/wav;base64,"+escaped);
        }
        var bytes=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(bytes);
        Assert.DoesNotContain(escaped,actual); Assert.DoesNotContain("QUJD%52%45VGSA",actual);
        Assert.Equal("prefix <omitted: 12 base64 chars> suffix",Assert.Single(Otlp.Spans(bytes)).Attributes["media"]);
    }
    public static IEnumerable<object[]> CredentialCases=>from framework in new[] { "net8.0","net10.0" }
        from credential in new[] { "aws","body" } from layer in new[] { "wire","library","privacy_only" }
        select new object[] { framework,credential,layer };
    [Theory]
    [MemberData(nameof(CredentialCases))]
    public async Task RealHookDiscoversAwsAndBodyCredentialsEvenWithCoveredNativeDescendants(string framework,string credential,string layer)
    {
        string secret=credential=="aws"?"AWS_SESSION_SECRET_9876":"MCP_BODY_SECRET_9876";
        byte[] body=Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"bad "+secret+"\"}}");
        await using var server=new MockServer(body,"application/json") { StatusCode=401 };
        var environment=EnvironmentFor(server); if(layer=="privacy_only") environment["FI_WIRE_CAPTURE"]="false";
        string mode="round4_credentials_"+credential+"_"+layer;
        var baselineEnvironment=new Dictionary<string,string?>(environment) { ["DOTNET_STARTUP_HOOKS"]=null };
        var baseline=await StartupHookTests.RunApp(server.Url+"v1/messages",mode,baselineEnvironment,framework);
        var traced=await StartupHookTests.RunApp(server.Url+"v1/messages",mode,environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        Assert.Equal("response=401;body="+Convert.ToBase64String(body),traced.Output.Trim());
        byte[] bytes=Assert.Single(server.Exports); Assert.DoesNotContain(secret,Encoding.UTF8.GetString(bytes));
        Assert.Equal(layer=="wire"?"wire":"library",Assert.Single(Otlp.Spans(bytes)).Attributes["traceai.capture.layer"]);
        using var status=System.Text.Json.JsonDocument.Parse(Assert.Single(Otlp.Extras(bytes,15)));
        Assert.Contains("<redacted>",status.RootElement.GetProperty("Description").GetString());
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookHidesDotTextAndPercentEscapedMediaOnBothRuntimes(string framework)
    {
        await using var collector=new MockServer([],"application/json");
        var environment=EnvironmentFor(collector); environment["FI_HIDE_INPUT_TEXT"]="true"; environment["FI_HIDE_OUTPUT_TEXT"]="true";
        var child=await StartupHookTests.RunApp(collector.Url,"round4_second_privacy",environment,framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error); Assert.Equal("privacy-complete",child.Output.Trim());
        byte[] bytes=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(bytes);
        Assert.DoesNotContain("PRIVATE_DOT_TEXT",actual); Assert.DoesNotContain("%51%55%4A%44",actual); Assert.DoesNotContain("AWS_SESSION_SECRET_9876",actual);
        var safe=Assert.Single(Otlp.Spans(bytes)); Assert.Equal(TraceConfig.RedactedValue,safe.Attributes["input.text"]);
        Assert.Equal(TraceConfig.RedactedValue,safe.Attributes["http.request.header.x_amz_security_token"]);
    }
    public static IEnumerable<object[]> ExactCases=>from framework in new[] { "net8.0","net10.0" }
        from read in new[] { "sync","memory","array" } from binary in new[] { false,true }
        select new object[] { framework,read,binary };
    [Theory]
    [MemberData(nameof(ExactCases))]
    public async Task RealHookExactContentLengthExportsBeforeAnyEofReadOrDisposal(string framework,string read,bool binary)
    {
        byte[] bytes=binary?Enumerable.Range(0,257).Select(i=>(byte)i).ToArray():"{\"choices\":[{\"message\":{\"content\":\"all response bytes\"}}]}"u8.ToArray();
        await using var server=new MockServer(bytes,binary?"audio/mpeg":"application/json") { UseContentLength=true };
        string path=binary?"v1/audio/speech":"v1/chat/completions",mode="round4_exact_"+read;
        var environment=EnvironmentFor(server); var baselineEnvironment=new Dictionary<string,string?>(environment) { ["DOTNET_STARTUP_HOOKS"]=null };
        var baseline=await StartupHookTests.RunApp(server.Url+path,mode,baselineEnvironment,framework);
        var traced=await StartupHookTests.RunApp(server.Url+path,mode,environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        Assert.Equal(Convert.ToBase64String(bytes),traced.Output.Trim());
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans)); Assert.False(span.Attributes.ContainsKey("traceai.wire.incomplete"));
        if(binary) Assert.Equal(bytes.Length.ToString(),span.Attributes["traceai.audio.output_bytes"].ToString());
        else Assert.Equal("all response bytes",span.Attributes["output.value"]);
    }
    [Theory]
    [InlineData("many")] [InlineData("long")] [InlineData("nodes")] [InlineData("depth")] [InlineData("malformed")]
    public void BodyCredentialDiscoveryOverflowsFailClosedOnlyForTheRequest(string shape)
    {
        string body=shape switch {
            "many"=>"{\"mcp_servers\":["+string.Join(",",Enumerable.Range(0,129).Select(i=>"{\"authorization_token\":\"SECRET_"+i.ToString("D4")+"\"}"))+"]}",
            "long"=>"{\"authorization_token\":\""+new string('s',4097)+"\"}",
            "nodes"=>"{\"items\":["+string.Join(",",Enumerable.Repeat("null",PrivacyCatalog.MaxNodes+1))+"],\"authorization_token\":\"LATE_SECRET_9876\"}",
            "depth"=>new string('[',65)+"{\"token\":\"DEEP_SECRET_9876\"}"+new string(']',65),
            _=>"{\"token\":\"INCOMPLETE_SECRET_9876" };
        var request=new RequestData { Body=Encoding.UTF8.GetBytes(body) };
        var secrets=WirePrivacy.RequestSecrets(request);
        Assert.True(request.CredentialDiscoveryIncomplete);
        Assert.All(secrets,secret=>Assert.InRange(secret.Length,8,KnownSecrets.MaxLength));
        KnownSecrets.Register(secrets); Assert.NotNull(KnownSecrets.Snapshot());
        using var original=new Activity("unrelated later call").Start(); original.SetTag("output.value","unrelated answer"); original.Stop();
        var batch=new Batch<Activity>([original],1); var snapshot=new AutoProcessor(Config()).PrepareExport(in batch,Resource.Empty);
        var safe=Assert.Single(snapshot.Spans); Assert.Equal("unrelated answer",safe.GetTagItem("output.value"));
        Assert.Equal(original.TraceId,safe.TraceId); Assert.Equal(original.Duration,safe.Duration);
    }
    private sealed class NeverSerializeContent : HttpContent
    {
        internal int Calls;
        protected override bool TryComputeLength(out long length) { Calls++; throw new InvalidOperationException("application length"); }
        protected override Task SerializeToStreamAsync(Stream stream,System.Net.TransportContext? context)
        { Calls++; throw new InvalidOperationException("application serializer"); }
        protected override Task<Stream> CreateContentReadStreamAsync()
        { Calls++; throw new InvalidOperationException("application stream factory"); }
    }
    [Fact]
    public void PrivacyOnlyBodyCredentialDiscoveryNeverInvokesCustomContentOrItsLength()
    {
        using var content=new NeverSerializeContent();
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://api.anthropic.com/v1/messages") { Content=content };
        request.Headers.Add("X-Amz-Security-Token","AWS_SESSION_SECRET_9876");
        using var model=new Activity("model").Start(); model.SetTag("gen_ai.span.kind","LLM");
        using var capture=new HttpCapture(Config());
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        Assert.Equal(0,content.Calls); Assert.Same(content,request.Content);
        Assert.Contains("AWS_SESSION_SECRET_9876",KnownSecrets.Snapshot());
        Assert.Null(model.GetCustomProperty("traceai.wire.secrets"));
    }
    [Fact]
    public void CoveredBufferedBodyOverTheCaptureLimitDoesNotDisableTheRegistry()
    {
        var options=new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",WireCapture=false,WireMaxBodyBytes=0,
            ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") };
        using var model=new Activity("model").Start(); model.SetTag("gen_ai.span.kind","LLM");
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://api.anthropic.com/v1/messages") {
            Content=new StringContent("{\"mcp_servers\":[{\"authorization_token\":\"BODY_SECRET_9876\"}]}",Encoding.UTF8,"application/json") };
        using var capture=new HttpCapture(new AutoConfig(options));
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        Assert.NotNull(KnownSecrets.Snapshot());
        Assert.Null(model.GetCustomProperty("traceai.wire.secrets"));
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void ExactLengthCompletionAlsoPublishesTruncatedAndBinaryBodies(bool binary)
    {
        var memory=new MemoryExporter(); using var agent=new Agent(Config(exporter:memory));
        var request=new RequestData { Url="https://api.openai.com/v1/"+(binary?"audio/speech":"chat/completions"),
            Body=Encoding.UTF8.GetBytes(binary?"{\"input\":\"hello\"}":"{\"messages\":[]}") };
        var capture=new Capture(WireCore.Match("POST",request.Url)!,request,1,default,DateTimeOffset.UtcNow,[]);
        capture.Response.Status=200; capture.Response.Headers["Content-Type"]=binary?"audio/mpeg":"application/json";
        capture.Response.Headers["Content-Length"]="4";
        capture.Bytes("ab"u8); Assert.True(agent.Flush(3000)); Assert.Empty(memory.Spans);
        DateTime before=DateTime.UtcNow; capture.Bytes("cd"u8); DateTime after=DateTime.UtcNow;
        Assert.True(agent.Flush(3000)); var span=Assert.Single(memory.Spans);
        Assert.InRange(span.StartTimeUtc+span.Duration,before,after); Assert.Null(span.GetTagItem("traceai.wire.incomplete"));
        if(binary) Assert.Equal(4L,span.GetTagItem("traceai.audio.output_bytes"));
        else Assert.Equal(true,span.GetTagItem("traceai.wire.truncated"));
        capture.Complete(); Assert.True(agent.Flush(3000)); Assert.Single(memory.Spans);
    }
    [Fact]
    public async Task ExactLengthCompletionIsObservableByFlushWhileResponseAndStreamStayOpen()
    {
        var memory=new MemoryExporter(); using var agent=new Agent(Config(exporter:memory));
        byte[] bytes="{\"choices\":[{\"message\":{\"content\":\"complete before dispose\"}}]}"u8.ToArray();
        await using var server=new MockServer(bytes,"application/json") { UseContentLength=true };
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") {
            Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
        await using var stream=await response.Content.ReadAsStreamAsync();
        var received=new byte[bytes.Length]; await stream.ReadExactlyAsync(received);
        Assert.Equal(bytes,received); Assert.True(agent.Flush(3000));
        var span=Assert.Single(memory.Spans); Assert.Equal("complete before dispose",span.GetTagItem("output.value"));
        Assert.Null(span.GetTagItem("traceai.wire.incomplete"));
    }
}
