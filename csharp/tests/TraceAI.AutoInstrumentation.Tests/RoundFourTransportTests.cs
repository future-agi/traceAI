using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFourTransportTests
{
    private static AutoConfig Config(BaseExporter<Activity> exporter)=>new(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    public static IEnumerable<object[]> Baselines=>from framework in new[] { "net8.0","net10.0" }
        from mode in new[] { "copy","buffered","sync","zero","cancel","partial","unread","invalid" }
        from framing in new[] { "json","sse" } select new object[] { framework,mode,framing };
    [Theory]
    [MemberData(nameof(Baselines))]
    public async Task RealStartupHookMatchesNoAgentResponseAndStreamBaseline(string framework,string mode,string framing)
    {
        byte[] bytes=framing=="json"?"{\"choices\":[{\"message\":{\"content\":\"same bytes\"}}]}"u8.ToArray():
            "data: {\"choices\":[{\"delta\":{\"content\":\"same bytes\"}}]}\n\ndata: [DONE]\n\n"u8.ToArray();
        await using var server=new MockServer(bytes,framing=="json"?"application/json":"text/event-stream") { UseContentLength=framing=="json" };
        var env=new Dictionary<string,string?> { ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
        var baselineEnv=new Dictionary<string,string?>(env) { ["DOTNET_STARTUP_HOOKS"]=null };
        var baseline=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","round4_parity_"+mode,baselineEnv,framework);
        var traced=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","round4_parity_"+mode,env,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans)); Assert.Equal("wire",span.Attributes["traceai.capture.layer"]);
        if(mode is "unread" or "partial") Assert.Equal(true,span.Attributes["traceai.wire.incomplete"]);
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookBootstrapInitializesOnlyOnceAndLogsOneRepeatLine(string framework)
    {
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        var child=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","round4_once",new() {
            ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url },framework);
        Assert.Equal(0,child.ExitCode); Assert.Equal("{}",child.Output.Trim());
        Assert.Equal(1,child.Error.Split("Agent already initialized").Length-1);
        Assert.Single(server.Exports.SelectMany(Otlp.Spans));
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookSanitizesScopeTracestateEventsAndLinksOnBothSupportedRuntimes(string framework)
    {
        await using var collector=new MockServer([],"application/json");
        var child=await StartupHookTests.RunApp(collector.Url,"round4_privacy",new() {
            ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=collector.Url,["FI_HIDE_INPUTS"]="true" },framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error);
        byte[] bytes=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(bytes);
        foreach(var secret in new[] { "PRIVATE_","explicit-api-key","explicit-secret-key" }) Assert.DoesNotContain(secret,actual);
        var span=Assert.Single(Otlp.Spans(bytes)); Assert.Equal(TraceConfig.RedactedValue,span.Attributes["input.value"]); Assert.Equal("NaN",span.Attributes["nonfinite"]);
        Assert.NotEmpty(Otlp.Extras(bytes,11)); Assert.NotEmpty(Otlp.Extras(bytes,13));
    }
    [Theory]
    [InlineData("LLM",false)] [InlineData("EMBEDDING",false)] [InlineData("RERANKER",false)] [InlineData("GUARDRAIL",false)]
    [InlineData("AGENT",true)] [InlineData("CHAIN",true)] [InlineData("TOOL",true)]
    public async Task DeduplicationSurvivesTwoIntermediateNativeActivities(string kind,bool wire)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using var parent=new Activity("framework").Start(); parent.ActivityTraceFlags=ActivityTraceFlags.Recorded; parent.SetTag("gen_ai.span.kind",kind);
        using var native=new Activity("native sdk client").Start(); using var deeper=new Activity("native retry").Start();
        using var client=new HttpClient(); using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
        Assert.Equal("{}",await response.Content.ReadAsStringAsync()); Assert.True(agent.Flush(3000));
        if(wire) Assert.Single(exporter.Spans); else Assert.Empty(exporter.Spans);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task DeduplicationTracksExplicitParentContextsAndLateModelKind(bool lateKind)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using var models=new ActivitySource("OpenAI.Round4.ContextModel"); using var nativeSource=new ActivitySource("Native.Round4.Client");
        using var listener=new ActivityListener { ShouldListenTo=s=>s.Name==nativeSource.Name,Sample=(ref ActivityCreationOptions<ActivityContext> _)=>ActivitySamplingResult.AllDataAndRecorded };
        ActivitySource.AddActivityListener(listener);
        var ambient=Activity.Current;
        try
        {
            using var model=models.StartActivity(lateKind?"model wrapper":"chat model"); Assert.NotNull(model);
            if(lateKind) model.SetTag("gen_ai.span.kind","LLM");
            Activity.Current=null;
            using var native=nativeSource.StartActivity("native client",ActivityKind.Client,model.Context); Assert.NotNull(native); Assert.Null(native.Parent);
            Activity.Current=null;
            using var deeper=nativeSource.StartActivity("native retry",ActivityKind.Client,native.Context); Assert.NotNull(deeper); Assert.Null(deeper.Parent);
            using var client=new HttpClient(); using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            Assert.Equal("{}",await response.Content.ReadAsStringAsync());
        }
        finally { Activity.Current=ambient; }
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans); Assert.Equal("library",span.GetTagItem("traceai.capture.layer"));
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void TransportEndTimePrecedesParsingAndActivityStartCallbacks(bool error)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        var request=new RequestData { Url="https://api.openai.com/v1/chat/completions",Body="{\"messages\":[]}"u8.ToArray() };
        var started=DateTimeOffset.UtcNow;
        var capture=new Capture(WireCore.Match("POST",request.Url)!,request,4194304,default,started,[]);
        capture.Response.Status=200; capture.Response.Headers["Content-Type"]="application/json";
        DateTime before=DateTime.UtcNow;
        capture.Bytes("{\"choices\":[{\"message\":{\"content\":\"transport end\"}}]}"u8);
        DateTime after=DateTime.UtcNow,callback=default;
        using var listener=new ActivityListener { ShouldListenTo=s=>s.Name==TraceAIAuto.Source.Name,ActivityStarted=_=>callback=DateTime.UtcNow };
        ActivitySource.AddActivityListener(listener);
        if(error) { before=DateTime.UtcNow; capture.Complete(new IOException("transport error")); after=callback; }
        else capture.Complete();
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal(started.UtcDateTime,span.StartTimeUtc);
        Assert.InRange(span.StartTimeUtc+span.Duration,before,after); Assert.True(span.StartTimeUtc+span.Duration<=callback);
    }
    private sealed class MessageTrapException() : IOException("transport failure explicit-api-key")
    {
        internal int Calls;
        public override string Message { get { Calls++; throw new InvalidOperationException("application getter must not run"); } }
    }
    private sealed class ErrorStream(Exception failure) : MemoryStream
    { public override int Read(byte[] buffer,int offset,int count)=>throw failure; }
    [Fact]
    public void TransportErrorsNeverInvokeOverriddenMessageGetters()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        var request=new RequestData { Url="https://api.openai.com/v1/chat/completions",Body="{\"messages\":[]}"u8.ToArray() };
        var capture=new Capture(WireCore.Match("POST",request.Url)!,request,4194304,default,DateTimeOffset.UtcNow,[]);
        var failure=new MessageTrapException(); using var stream=new ReadTapStream(new ErrorStream(failure),capture);
        Assert.Same(failure,Record.Exception(()=>stream.Read(new byte[1],0,1)));
        Assert.True(agent.Flush(3000)); Assert.Equal(0,failure.Calls);
        Assert.Equal("transport failure <redacted>",Assert.Single(exporter.Spans).StatusDescription);
    }
    private sealed class CustomContent : HttpContent
    {
        internal int Reads;
        internal readonly SpecialStream Stream=new();
        internal sealed class SpecialStream : MemoryStream { internal string Helper()=>"subclass helper"; }
        protected override bool TryComputeLength(out long length) { length=0; return true; }
        protected override Task SerializeToStreamAsync(Stream stream,System.Net.TransportContext? context) { Reads++; return Task.CompletedTask; }
        protected override Task<Stream> CreateContentReadStreamAsync() { Reads++; return Task.FromResult<Stream>(Stream); }
    }
    [Fact]
    public async Task UnsupportedCustomContentAndSubclassStreamsRemainUntouched()
    {
        var exporter=new MemoryExporter(); using var capture=new HttpCapture(Config(exporter));
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://api.openai.com/v1/chat/completions") { Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        using var content=new CustomContent(); using var response=new HttpResponseMessage(System.Net.HttpStatusCode.OK) { Content=content };
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Stop",new { Request=request,Response=response }));
        Assert.Same(content,response.Content); Assert.Equal(0,content.Reads);
        Assert.Same(content.Stream,await response.Content.ReadAsStreamAsync()); Assert.Equal("subclass helper",content.Stream.Helper()); Assert.Equal(1,content.Reads);
    }
}
