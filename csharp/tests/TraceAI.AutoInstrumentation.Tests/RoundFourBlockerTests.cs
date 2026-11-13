using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFourBlockerTests : InitIsolation
{
    private static AutoConfig Config()=>new(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    private static Dictionary<string,string?> EnvironmentFor(MockServer server)=>new() {
        ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
    [Theory]
    [InlineData("key")] [InlineData("%6bey")] [InlineData("api_key")] [InlineData("access_token")]
    [InlineData("x-amz-signature")] [InlineData("client_secret")]
    public void QueryCredentialDiscoveryIncludesEncodedAndDecodedValues(string parameter)
    {
        var request=new RequestData { Url="https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?ignored=harmless&"+parameter+"=QUERY_SECRET%2B9876&model=public-model#fragment" };
        var secrets=WirePrivacy.RequestSecrets(request);
        Assert.Contains("QUERY_SECRET%2B9876",secrets); Assert.Contains("QUERY_SECRET+9876",secrets);
        Assert.DoesNotContain("harmless",secrets); Assert.DoesNotContain("public-model",secrets);
        Assert.Equal("echo <redacted>",WirePrivacy.ScrubString("echo QUERY_SECRET+9876",secrets));
    }
    [Theory]
    [InlineData("long")] [InlineData("many")] [InlineData("parameters")]
    public void QueryCredentialOverflowFailsClosedOnlyForTheRequest(string shape)
    {
        string query=shape=="long"?"key="+new string('s',4097):shape=="many"?
            string.Join("&",Enumerable.Range(0,129).Select(i=>"key=SECRET_"+i.ToString("D4"))):
            string.Join("&",Enumerable.Repeat("unrelated=x",4097));
        var request=new RequestData { Url="https://example.test/v1/chat/completions?"+query,Body="{\"model\":\"m\",\"messages\":[]}"u8.ToArray() };
        KnownSecrets.Register(WirePrivacy.QuerySecrets(request.Url,request));
        Assert.True(request.CredentialDiscoveryIncomplete); Assert.NotNull(KnownSecrets.Snapshot());
        var wire=WireCore.Build(WireCore.Match("POST",request.Url)!,request,new ResponseData { Status=200,Body="{\"choices\":[{\"message\":{\"content\":\"private\"}}]}"u8.ToArray() })!;
        Assert.False(wire.Attributes.ContainsKey("output.value")); Assert.Equal("LLM",wire.Attributes["gen_ai.span.kind"]);
        Assert.Equal(true,wire.Attributes["traceai.wire.incomplete"]);
        using var activity=new Activity("unrelated later call").Start(); activity.SetTag("note","unrelated answer"); activity.Stop();
        var batch=new Batch<Activity>([activity],1); var snapshot=new AutoProcessor(Config()).PrepareExport(in batch,Resource.Empty);
        var safe=Assert.Single(snapshot.Spans);
        Assert.Equal("unrelated answer",safe.GetTagItem("note")); Assert.Equal(activity.TraceId,safe.TraceId); Assert.Equal(activity.Duration,safe.Duration);
    }
    public static IEnumerable<object[]> HeaderCases=>from header in new[] {
        "Authorization","x-api-key","api-key","xi-api-key","x-goog-api-key","anthropic-api-key","Ocp-Apim-Subscription-Key","X-Amz-Security-Token" }
        from compatibility in new[] { false,true } select new object[] { header,compatibility };
    [Theory]
    [MemberData(nameof(HeaderCases))]
    public async Task RecognizedAuthHeadersAreRedactedOnEveryExportSurfaceWithoutAnOutgoingRequest(string header,bool compatibility)
    {
        const string secret="SUBSCRIPTION_SECRET_9876";
        string key="http.request.header."+header;
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config());
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions {
            Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("Round4.HeaderBoundary",tags:new Dictionary<string,object?> { [key]=new[] { secret } });
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string,object> { [key]=new[] { secret } }))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=source.StartActivity("auth header",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),new ActivityTagsCollection { [key]=new[] { secret } }) }))
        {
            Assert.NotNull(span); span.SetTag(key,new[] { secret });
            span.SetTag("http.request.header."+header.Replace('-','_'),new[] { secret });
            span.AddEvent(new ActivityEvent("event",tags:new ActivityTagsCollection { [key]=new[] { secret } }));
        }
        byte[] bytes=Assert.Single(collector.Exports); Assert.DoesNotContain(secret,Encoding.UTF8.GetString(bytes));
        var decoded=Assert.Single(Otlp.Spans(bytes)); Assert.Equal(TraceConfig.RedactedValue,decoded.Attributes[key]);
        Assert.Equal(TraceConfig.RedactedValue,decoded.Resource[key]);
        Assert.NotEmpty(Otlp.Extras(bytes,11)); Assert.NotEmpty(Otlp.Extras(bytes,13));
    }
    public static IEnumerable<object[]> QueryCases=>from framework in new[] { "net8.0","net10.0" }
        from layer in new[] { "wire","library","privacy_only" } select new object[] { framework,layer };
    [Theory]
    [MemberData(nameof(QueryCases))]
    public async Task RealHookQueryCredentialsCannotLeakThroughWireOrCoveredLibraryAncestor(string framework,string layer)
    {
        const string secret="PROVIDER_QUERY_SECRET_9876";
        byte[] body=Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"bad "+secret+"\"}}");
        await using var server=new MockServer(body,"application/json") { StatusCode=401 };
        var environment=EnvironmentFor(server); if(layer=="privacy_only") environment["FI_WIRE_CAPTURE"]="false";
        var child=await StartupHookTests.RunApp(server.Url+"v1beta/models/m:generateContent?key="+secret,"round4_query_"+layer,environment,framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error);
        Assert.Equal("response=401;body="+Convert.ToBase64String(body),child.Output.Trim());
        var bytes=Assert.Single(server.Exports); Assert.DoesNotContain(secret,Encoding.UTF8.GetString(bytes));
        var span=Assert.Single(Otlp.Spans(bytes)); Assert.Equal(layer=="wire"?"wire":"library",span.Attributes["traceai.capture.layer"]);
        using var status=System.Text.Json.JsonDocument.Parse(Assert.Single(Otlp.Extras(bytes,15)));
        Assert.Contains("<redacted>",status.RootElement.GetProperty("Description").GetString());
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookAuthHeaderStringArrayIsRedactedWithoutAnOutgoingRequest(string framework)
    {
        await using var server=new MockServer([],"application/json");
        var child=await StartupHookTests.RunApp(server.Url,"round4_header",EnvironmentFor(server),framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error); Assert.Equal("header-complete",child.Output.Trim());
        var bytes=Assert.Single(server.Exports); Assert.DoesNotContain("SUBSCRIPTION_SECRET_9876",Encoding.UTF8.GetString(bytes));
        Assert.Equal(TraceConfig.RedactedValue,Assert.Single(Otlp.Spans(bytes)).Attributes["http.request.header.Ocp-Apim-Subscription-Key"]);
    }
    [DllImport("libc",EntryPoint="kill",SetLastError=true)]
    private static extern int Signal(int processId,int signal);
    public static IEnumerable<object[]> TermCases=>from framework in new[] { "net8.0","net10.0" }
        from layer in new[] { "wire","library" } from cancel in new[] { false,true } select new object[] { framework,layer,cancel };
    [Theory]
    [MemberData(nameof(TermCases))]
    public async Task RealHookSigtermFlushesCompletedSpansWithoutChangingDefaultOrCanceledTermination(string framework,string layer,bool cancel)
    {
        if(OperatingSystem.IsWindows()) return; // SIGTERM and libc do not exist on Windows.
        await using var baselineServer=new MockServer("{}"u8.ToArray(),"application/json");
        await using var tracedServer=new MockServer("{}"u8.ToArray(),"application/json");
        string mode="round4_term_"+(cancel?"cancel_":"default_")+layer;
        var baseline=await Terminate(baselineServer,mode,framework,hook:false,cancel);
        var traced=await Terminate(tracedServer,mode,framework,hook:true,cancel);
        Assert.Equal(baseline.ExitCode,traced.ExitCode); Assert.Equal(cancel?0:143,traced.ExitCode);
        Assert.Equal(baseline.Output,traced.Output); Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error);
        Assert.Empty(baselineServer.Exports);
        var spans=tracedServer.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(cancel?3:1,spans.Length);
        Assert.All(spans,span=>Assert.Equal(layer,span.Attributes["traceai.capture.layer"]));
        Assert.Equal(spans.Length,spans.Select(span=>span.Id).Distinct().Count());
    }
    private static async Task<StartupHookTests.ChildResult> Terminate(MockServer server,string mode,string framework,bool hook,bool cancel)
    {
        var environment=EnvironmentFor(server); if(!hook) environment["DOTNET_STARTUP_HOOKS"]=null;
        using var process=StartupHookTests.StartApp(server.Url+"v1/chat/completions",mode,environment,framework);
        var stderr=process.StandardError.ReadToEndAsync(); var lines=new List<string?>();
        async Task<string?> ReadLine()=>await process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(15));
        try
        {
            lines.Add(await ReadLine()); Assert.Equal("ready",lines[^1]);
            // Signal immediately after body consumption/model Stop, not after a batch delay.
            Assert.Equal(0,Signal(process.Id,15));
            if(cancel)
            {
                lines.Add(await ReadLine()); Assert.Equal("continued",lines[^1]);
                Assert.Equal(0,Signal(process.Id,15));
                lines.Add(await ReadLine()); Assert.Equal("continued",lines[^1]);
                await process.StandardInput.WriteLineAsync("exit"); await process.StandardInput.FlushAsync();
            }
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(8));
            string remainder=await process.StandardOutput.ReadToEndAsync(); Assert.Equal("",remainder);
            return new(process.ExitCode,string.Join("\n",lines),await stderr);
        }
        finally
        {
            if(!process.HasExited) { process.Kill(true); await process.WaitForExitAsync(); }
        }
    }
}
