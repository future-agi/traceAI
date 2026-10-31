using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class IndependentVerifierRegressionTests
{
    private static AutoConfig Config(BaseExporter<Activity> exporter) => new(new AutoOptions {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","no-such-config.toml") });

    [Theory]
    [InlineData("net8.0")]
    [InlineData("net10.0")]
    public async Task InheritedEnvironmentResourcesAreScrubbedByTheRealStartupHook(string framework)
    {
        await using var server=new MockServer("{\"choices\":[]}"u8.ToArray(),"application/json");
        string media=new('A',512);
        var result=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","json",new Dictionary<string,string?> {
            ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url,
            ["FI_PROJECT_NAME"]="project-explicit-api-key",["FI_PROJECT_VERSION_ID"]="explicit-secret-key",
            ["OTEL_SERVICE_NAME"]="service-explicit-api-key",
            ["OTEL_RESOURCE_ATTRIBUTES"]="deployment.note=explicit-api-key,authentication.api_key=ENV_RESOURCE_SECRET_9876,"+
                "extra.json={\"password\":\"RESOURCE_JSON_SECRET_9876\"},media.json={\"data\":\""+media+"\"},"+
                "signed.url=https://example.invalid/path?token=RESOURCE_URL_SECRET_9876,"+
                "service.namespace=namespace-explicit-secret-key,resource.explicit-api-key.note=explicit-secret-key,safe.note=retained"
        },framework,stock:true);
        Assert.True(result.ExitCode==0,result.Error); Assert.Equal("",result.Error);
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans));
        var resource=span.Resource; string rendered=WireCore.J(resource);
        foreach(var secret in new[] { "explicit-api-key","explicit-secret-key","ENV_RESOURCE_SECRET_9876","RESOURCE_JSON_SECRET_9876","RESOURCE_URL_SECRET_9876",media })
            Assert.DoesNotContain(secret,rendered);
        Assert.Equal("<redacted>",resource["deployment.note"]);
        Assert.Equal(TraceConfig.RedactedValue,resource["authentication.api_key"]);
        Assert.Equal("{\"password\":\"<redacted>\"}",resource["extra.json"]);
        Assert.Equal("{\"data\":\"<omitted: 512 base64 chars>\"}",resource["media.json"]);
        // S6 classifies the signed component itself as credential-bearing.
        Assert.Equal(TraceConfig.RedactedValue,resource["signed.url"]);
        Assert.Equal("namespace-<redacted>",resource["service.namespace"]);
        Assert.Equal(TraceConfig.RedactedValue,resource["resource.<redacted>.note"]);
        Assert.Equal("service-<redacted>",resource["service.name"]);
        Assert.Equal("project-<redacted>",resource["project_name"]);
        Assert.Equal("<redacted>",resource["project_version_id"]);
        Assert.Equal("retained",resource["safe.note"]);
        Assert.Equal("opentelemetry",resource["telemetry.sdk.name"]);
    }

    [Theory]
    [InlineData("single-cycle")]
    [InlineData("branching-cycle")]
    [InlineData("mutual-cycle")]
    [InlineData("shared-dag")]
    public void ActivityStopFailsClosedForCyclicAndExpandingArrayGraphs(string shape)
    {
        object[] graph;
        switch(shape)
        {
            case "single-cycle": graph=new object[1]; graph[0]=graph; break;
            case "branching-cycle": graph=new object[2]; graph[0]=graph; graph[1]=graph; break;
            case "mutual-cycle":
                graph=new object[2]; var child=new object[] { graph,graph }; graph[0]=child; graph[1]=child; break;
            default:
                graph=new object[] { "safe" };
                for(int i=0;i<20;i++) graph=new object[] { graph,graph };
                break;
        }
        var first=graph[0];
        Assert.Equal(WirePrivacy.Redacted,WirePrivacy.Scrub(graph));
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using(var activity=TraceAIAuto.Source.StartActivity("array graph"))
        { Assert.NotNull(activity); activity.SetTag("graph",graph); }
        Assert.True(agent.Flush(3000));
        Assert.Equal(WirePrivacy.Redacted,Assert.Single(exporter.Spans).GetTagItem("graph"));
        Assert.Same(first,graph[0]); // Privacy must not mutate the application's graph.
    }

    [Fact]
    public void ArrayTraversalBudgetIsGlobalAndIncludesNullsBeforeAllocation()
    {
        var fitting=new object?[WirePrivacy.MaxAttributeNodes-1];
        Assert.Equal(fitting.Length,Assert.IsType<object[]>(WirePrivacy.Scrub(fitting)).Length);
        var oversized=new object?[WirePrivacy.MaxAttributeNodes];
        Assert.Equal(WirePrivacy.Redacted,WirePrivacy.Scrub(oversized));
        var shared=new object[] { new object?[WirePrivacy.MaxAttributeNodes/2],new object?[WirePrivacy.MaxAttributeNodes/2] };
        Assert.Equal(WirePrivacy.Redacted,WirePrivacy.Scrub(shared));
        var safe=Assert.IsType<object[]>(WirePrivacy.Scrub(new object[] { new[] { "safe","explicit-api-key" },42,true },new[] { "explicit-api-key" }));
        Assert.Equal(new[] { "safe","<redacted>" },Assert.IsType<string[]>(safe[0]));
        Assert.Equal(42,safe[1]); Assert.Equal(true,safe[2]);
    }

    // A gate, not a sleep: an accidental ToString cannot hang the runner, and a
    // completed Activity.Stop/export establishes that no user callback ran.
    private sealed class BlockingString : IDisposable
    {
        internal int Calls;
        internal readonly ManualResetEventSlim Entered=new(),Release=new();
        public override string ToString() { Interlocked.Increment(ref Calls); Entered.Set(); Release.Wait(); return "USER_CODE_MUST_NOT_RUN"; }
        public void Dispose() { Release.Set(); Entered.Dispose(); Release.Dispose(); }
    }
    private sealed class Enrich(Action<Activity> action) : BaseProcessor<Activity>
    { public override void OnEnd(Activity activity)=>action(activity); }

    [Theory]
    [InlineData("start")]
    [InlineData("stop")]
    [InlineData("export")]
    public async Task UnsupportedSemanticAttributesNeverInvokeUserToString(string phase)
    {
        using var value=new BlockingString();
        var exporter=new MemoryExporter(); var privacy=new AutoProcessor(Config(exporter));
        void Attach(Activity activity)
        {
            activity.SetTag("gen_ai.operation.name",value);
            activity.SetTag("gen_ai.span.kind",value);
            activity.SetTag("traceai.capture.layer",value);
        }
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(privacy).AddProcessor(new Enrich(a=> { if(phase=="export") Attach(a); }))
            .AddProcessor(new SimpleActivityExportProcessor(new BorrowedExporter(exporter,privacy,new ExportCompletion()))).Build();
        var work=Task.Run(()=> {
            var tags=phase=="start"?new ActivityTagsCollection {
                ["gen_ai.operation.name"]=value,["gen_ai.span.kind"]=value,["traceai.capture.layer"]=value }:null;
            using var activity=TraceAIAuto.Source.StartActivity("custom",ActivityKind.Internal,default(ActivityContext),tags);
            Assert.NotNull(activity); if(phase=="stop") Attach(activity);
        });
        try { await work.WaitAsync(TimeSpan.FromSeconds(10)); }
        finally { value.Release.Set(); await work.WaitAsync(TimeSpan.FromSeconds(10)); }
        Assert.Equal(0,value.Calls); Assert.False(value.Entered.IsSet);
        var span=Assert.Single(exporter.Spans);
        foreach(var key in new[] { "gen_ai.operation.name","gen_ai.span.kind","traceai.capture.layer" })
            Assert.Equal(typeof(BlockingString).FullName,span.GetTagItem(key));
    }

    [Fact]
    public async Task HttpClientDeduplicationNeverRendersUnsupportedSemanticAttributes()
    {
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using var value=new BlockingString();
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using var source=new ActivitySource("OpenAI.Semantics.Privacy");
        var work=Task.Run(async()=> {
            using var parent=source.StartActivity("framework"); Assert.NotNull(parent);
            parent.SetTag("gen_ai.operation.name",value); parent.SetTag("gen_ai.span.kind",value);
            using var client=new HttpClient();
            using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            await response.Content.ReadAsStringAsync();
        });
        try { await work.WaitAsync(TimeSpan.FromSeconds(10)); }
        finally { value.Release.Set(); await work.WaitAsync(TimeSpan.FromSeconds(10)); }
        Assert.True(agent.Flush(3000)); Assert.Equal(0,value.Calls); Assert.False(value.Entered.IsSet);
        Assert.Single(exporter.Spans,s=>s.GetTagItem("traceai.capture.layer") is "wire");
    }

    public static IEnumerable<object[]> StreamingCases =>
        from endpoint in new[] { "bedrock-converse","bedrock-invoke","vertex","gemini" }
        from streaming in new[] { false,true }
        from framing in new[] { "json","array","binary","sse","ndjson" }
        select new object[] { endpoint,streaming,framing };
    private static string EndpointUrl(string endpoint,bool streaming) => endpoint switch {
        "bedrock-converse"=>"https://bedrock-runtime.us-east-1.amazonaws.com/model/m/"+(streaming?"converse-stream":"converse"),
        "bedrock-invoke"=>"https://bedrock-runtime.us-east-1.amazonaws.com/model/m/"+(streaming?"invoke-with-response-stream":"invoke"),
        "vertex"=>"https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/publishers/anthropic/models/m:"+(streaming?"streamRawPredict":"rawPredict"),
        _=>"https://generativelanguage.googleapis.com/v1beta/models/m:"+(streaming?"streamGenerateContent":"generateContent")
    };
    [Theory]
    [MemberData(nameof(StreamingCases))]
    public void StreamingEndpointFlagsAndFirstChunkAreIndependentOfFraming(string endpoint,bool streaming,string framing)
    {
        string url=EndpointUrl(endpoint,streaming);
        var request=new RequestData { Url=url,Body=Encoding.UTF8.GetBytes(endpoint=="gemini"?"{\"contents\":[]}":"{\"messages\":[]}") };
        string contentType=framing switch { "binary"=>"application/vnd.amazon.eventstream","sse"=>"text/event-stream","ndjson"=>"application/x-ndjson",_=>"application/json" };
        var response=new ResponseData { Status=200,FirstChunkSeconds=0.123456789,
            Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-type"]=contentType },
            Body=Encoding.UTF8.GetBytes(framing switch { "sse"=>"data: {}\n\n","ndjson"=>"{}\n{}\n","array"=>"[{},{}]","binary"=>"BINARY_EVENT_BYTES",_=>"{}" }) };
        var match=WireCore.Match("POST",url); Assert.NotNull(match);
        var result=WireCore.Build(match,request,response); Assert.NotNull(result);
        if(streaming || framing is "sse" or "ndjson")
        {
            Assert.Equal(true,result.Attributes["gen_ai.request.stream"]);
            Assert.Equal(0.123457,result.Attributes["gen_ai.response.time_to_first_chunk"]);
        }
        else
        {
            Assert.False(result.Attributes.ContainsKey("gen_ai.request.stream"));
            Assert.False(result.Attributes.ContainsKey("gen_ai.response.time_to_first_chunk"));
        }
        if(framing=="binary") Assert.False(result.Attributes.ContainsKey("output.value"));
    }
    [Theory]
    [InlineData("bedrock-converse")]
    [InlineData("bedrock-invoke")]
    [InlineData("vertex")]
    [InlineData("gemini")]
    public void StreamingEndpointsDoNotInventFirstChunkMeasurementsForEmptyResponses(string endpoint)
    {
        string url=EndpointUrl(endpoint,true);
        var request=new RequestData { Url=url,Body=Encoding.UTF8.GetBytes(endpoint=="gemini"?"{\"contents\":[]}":"{\"messages\":[]}") };
        var result=WireCore.Build(WireCore.Match("POST",url)!,request,new ResponseData { Status=200 });
        Assert.NotNull(result); Assert.Equal(true,result.Attributes["gen_ai.request.stream"]);
        Assert.False(result.Attributes.ContainsKey("gen_ai.response.time_to_first_chunk"));
    }
}
