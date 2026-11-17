using System.Diagnostics;
using System.Text;
using System.Text.Json;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Trace;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>What the application itself sees must not change because the hook is present:
/// its sampling, its exporters' view of shared activities, its outgoing request headers
/// and its request latency. Future AGI receives only the AI spans and the web requests
/// that made an AI call.</summary>
public class HostIsolationTests : InitIsolation
{
    private const string JsonResponse="{\"id\":\"local-id\",\"model\":\"local-test\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello back\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";
    private static AutoConfig Config(MemoryExporter exporter)=>new(new AutoOptions {
        ApiKey="isolation-key",SecretKey="isolation-secret",Exporter=exporter,ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    private static Dictionary<string,string?> HookEnvironment(MockServer server)=>new() {
        ["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_BASE_URL"]=server.Url };
    [Fact]
    public void HostExporterOnASharedSourceSeesNoHookTagsOrContext()
    {
        var hostExporter=new MemoryExporter(); var agentExporter=new MemoryExporter();
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("OpenAI.SharedView").AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        using var agent=new Agent(Config(agentExporter));
        using var source=new ActivitySource("OpenAI.SharedView");
        using(ContextAttributes.UsingSession("shared-session"))
        using(ContextAttributes.UsingUser("shared-user"))
        using(ContextAttributes.UsingPromptTemplate("greeting",variables:new Dictionary<string,object> { ["name"]="template value" }))
        using(var activity=source.StartActivity("chat shared",ActivityKind.Client,default(ActivityContext),new ActivityTagsCollection {
            ["gen_ai.operation.name"]="chat",["gen_ai.system"]="openai",["gen_ai.usage.input_tokens"]=3L,["gen_ai.usage.output_tokens"]=2L }))
            Assert.NotNull(activity);
        Assert.True(agent.Flush(3000));
        var hostView=Assert.Single(hostExporter.Spans);
        Assert.DoesNotContain(hostView.TagObjects,pair=>pair.Key.StartsWith("traceai.",StringComparison.Ordinal));
        foreach(var key in new[] { "session.id","user.id","metadata","tag.tags","gen_ai.prompt.template.name","gen_ai.prompt.template.variables",
            "gen_ai.span.kind","gen_ai.provider.name","gen_ai.usage.total_tokens" })
            Assert.Null(hostView.GetTagItem(key));
        var agentView=Assert.Single(agentExporter.Spans);
        Assert.Equal("LLM",agentView.GetTagItem("gen_ai.span.kind")); Assert.Equal("library",agentView.GetTagItem("traceai.capture.layer"));
        Assert.Equal("openai",agentView.GetTagItem("gen_ai.provider.name")); Assert.Equal(5L,agentView.GetTagItem("gen_ai.usage.total_tokens"));
        Assert.Equal("shared-user",agentView.GetTagItem("user.id")); Assert.Equal("greeting",agentView.GetTagItem("gen_ai.prompt.template.name"));
    }
    public static IEnumerable<object[]> HostSamplers=>from framework in new[] { "net8.0","net10.0" }
        from mode in new[] { "host_sampling_off","host_sampling_ratio" } select new object[] { framework,mode };
    [Theory]
    [MemberData(nameof(HostSamplers))]
    public async Task ApplicationSamplingAndExportsAreUnchangedByTheHook(string framework,string mode)
    {
        // The application samples web requests with AlwaysOff, or 1% parent-based, while the
        // hook samples at 1.0. Trace ids are random, so each run reports what the
        // application's own sampler chose and the test checks the application kept to it.
        async Task<(int[] Counts,int SampledHeaders,int Downstream)> Run(bool hook)
        {
            await using var server=new MockServer("{}"u8.ToArray(),"application/json");
            var environment=HookEnvironment(server); if(!hook) environment["DOTNET_STARTUP_HOOKS"]=null;
            var child=await StartupHookTests.RunApp(server.Url,mode,environment,framework);
            Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error);
            using var document=JsonDocument.Parse(child.Output); var root=document.RootElement;
            int[] counts=new[] { "requests","observed","expected","recorded","exported" }.Select(name=>root.GetProperty(name).GetInt32()).ToArray();
            var headers=server.RequestHeaders.ToArray();
            return (counts,headers.Count(h=>h.TryGetValue("traceparent",out var value)&&value.EndsWith("-01",StringComparison.Ordinal)),headers.Length);
        }
        var baseline=await Run(hook:false); var traced=await Run(hook:true);
        foreach(var (counts,sampledHeaders,downstream) in new[] { baseline,traced })
        {
            int requests=counts[0],observed=counts[1],expected=counts[2],recorded=counts[3],exported=counts[4];
            Assert.Equal(200,requests); Assert.Equal(requests,observed); Assert.Equal(requests,downstream);
            // The application's sampled flag, its exports (request plus one child) and the
            // flag sent downstream follow only the application's sampler.
            Assert.Equal(expected,recorded); Assert.Equal(2*expected,exported); Assert.Equal(expected,sampledHeaders);
            if(mode=="host_sampling_off") Assert.Equal(0,exported);
        }
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task WebRequestSpansAreSentOnlyForRequestsThatMadeAnAiCall(string framework)
    {
        await using var server=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        var child=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","host_server_routes",HookEnvironment(server),framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error);
        using var output=JsonDocument.Parse(child.Output);
        Assert.Equal(JsonResponse,output.RootElement.GetProperty("ai").GetString()); Assert.Equal("plain",output.RootElement.GetProperty("plain").GetString());
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray();
        var wire=Assert.Single(spans,span=>span.Attributes.GetValueOrDefault("traceai.capture.layer") as string=="wire");
        var request=Assert.Single(spans,span=>span.Kind==2);
        Assert.Equal(request.Id,wire.ParentId);
        // Only the exact web request source is followed, not other framework sources under it.
        Assert.DoesNotContain(spans,span=>span.Name=="nested framework work");
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RequestsWithoutAnActivityGetNoTraceHeadersFromTheHook(string framework)
    {
        await using var baselineServer=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        await using var tracedServer=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        var baselineEnvironment=HookEnvironment(baselineServer); baselineEnvironment["DOTNET_STARTUP_HOOKS"]=null;
        var baseline=await StartupHookTests.RunApp(baselineServer.Url,"host_plain_headers",baselineEnvironment,framework);
        var traced=await StartupHookTests.RunApp(tracedServer.Url,"host_plain_headers",HookEnvironment(tracedServer),framework);
        foreach(var child in new[] { baseline,traced })
        { Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error); Assert.Equal("headers-complete",child.Output.Trim()); }
        var expected=baselineServer.RequestHeaders.ToArray(); var actual=tracedServer.RequestHeaders.ToArray();
        Assert.Equal(2,expected.Length); Assert.Equal(2,actual.Length);
        for(int i=0;i<expected.Length;i++)
            Assert.Equal(expected[i].Keys.Order(StringComparer.OrdinalIgnoreCase),actual[i].Keys.Order(StringComparer.OrdinalIgnoreCase));
        Assert.All(actual,headers=> { Assert.False(headers.ContainsKey("traceparent")); Assert.False(headers.ContainsKey("tracestate")); });
        // The AI call is still captured without an outgoing request activity.
        Assert.Single(tracedServer.Exports.SelectMany(Otlp.Spans),span=>span.Attributes.GetValueOrDefault("traceai.capture.layer") as string=="wire");
    }
    [Fact]
    public void HostContextLookupsDoNotAllocateForEachActivity()
    {
        using var agent=new Agent(Config(new MemoryExporter()));
        int Lookups(int count)
        {
            int seen=0;
            for(int i=0;i<count;i++)
            {
                if(InstrumentationSuppression.Active) seen++;
                if(HostContext.Suppressed) seen++;
                seen+=HostContext.Attributes().Count();
            }
            return seen;
        }
        Assert.Equal(0,Lookups(100));
        long before=GC.GetAllocatedBytesForCurrentThread();
        int found=Lookups(10_000);
        long allocated=GC.GetAllocatedBytesForCurrentThread()-before;
        Assert.Equal(0,found);
        Assert.True(allocated<16*1024,$"{allocated} bytes allocated by 10,000 suppression and context lookups");
    }
    [Fact]
    public async Task LargeNonAiRequestBodiesAreNotParsedBeforeTheRequestIsSent()
    {
        using var agent=new Agent(Config(new MemoryExporter()));
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        static StringContent Body(string secret)=>new("{\"client_secret\":\""+secret+"\",\"padding\":\""+new string('p',64*1024)+"\"}",Encoding.UTF8,"application/json");
        using var client=new HttpClient();
        using(var plain=await client.PostAsync(server.Url+"api/orders",Body("PLAIN_LARGE_BODY_SECRET_9876"))) await plain.Content.ReadAsByteArrayAsync();
        using(var model=await client.PostAsync(server.Url+"v1/chat/completions",Body("MODEL_LARGE_BODY_SECRET_9876"))) await model.Content.ReadAsByteArrayAsync();
        var secrets=KnownSecrets.Snapshot();
        Assert.DoesNotContain("PLAIN_LARGE_BODY_SECRET_9876",secrets);
        Assert.Contains("MODEL_LARGE_BODY_SECRET_9876",secrets);
    }
}
