using System.Diagnostics;
using System.Text;
using System.Text.Json;
using OpenTelemetry;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class DiscoveryIsolationTests : InitIsolation
{
    private sealed class EnvironmentScope : IDisposable
    {
        private readonly Dictionary<string,string?> _previous=new();
        internal EnvironmentScope(params (string Name,string? Value)[] values)
        {
            foreach(var (name,value) in values) { _previous[name]=Environment.GetEnvironmentVariable(name); Environment.SetEnvironmentVariable(name,value); }
        }
        public void Dispose() { foreach(var pair in _previous) Environment.SetEnvironmentVariable(pair.Key,pair.Value); }
    }
    private static string AbsentConfig=>Path.Combine(WireFixtureTests.Repo,"csharp/absent.toml");
    private const string Reply="{\"id\":\"r1\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"answer\"}}],\"usage\":{\"prompt_tokens\":9,\"completion_tokens\":3,\"total_tokens\":12}}";
    private const string Chat="{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"question\"}]}";
    private static string Records(int count)=>JsonSerializer.Serialize(Enumerable.Range(0,count).Select(i=>new { id=i,sku="product",quantity=2,note="ordinary order" }));
    public static IEnumerable<object[]> Requests=>from route in new[] { "exporter","provider","otlp" }
        from shape in new[] { "bulk","overflow","tools","chat-overflow" } select new object[] { route,shape };
    [Theory]
    [MemberData(nameof(Requests))]
    public async Task BufferedDiscoveryIsRequestLocalAndPreservesLaterSpansAndResource(string route,string shape)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("OTEL_SDK_DISABLED",null),
            ("OTEL_SERVICE_NAME","checkout-api"),("OTEL_RESOURCE_ATTRIBUTES","service.version=9.8.7"));
        await using var server=new MockServer(Encoding.UTF8.GetBytes(Reply),"application/json");
        await using var collector=new MockServer([],"application/json");
        var host=new MemoryExporter(); var exported=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name,"OpenAI.Discovery")
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string,object> { ["service.name"]="checkout-api" }))
            .AddProcessor(new SimpleActivityExportProcessor(host)).Build();
        string large=shape switch {
            "bulk"=>Records(1100),"overflow"=>Records(4000),
            "chat-overflow"=>Chat[..^1]+",\"extra\":"+Records(4000)+"}",
            _=>Chat[..^1]+",\"tools\":"+JsonSerializer.Serialize(Enumerable.Range(0,100).Select(i=>new {
                type="function",function=new { name="tool"+i,parameters=new { type="object",properties=Enumerable.Range(0,20).ToDictionary(j=>"field"+j,j=>new { type="string",description="tool field" }) } }
            }))+"}" };
        string path=shape is "bulk" or "overflow"?"api/orders/bulk":"v1/chat/completions";
        using var client=new HttpClient();
        async Task<(System.Net.HttpStatusCode Status,byte[] Body)> Send(string body,string endpoint)
        {
            using var response=await client.PostAsync(server.Url+endpoint,new StringContent(body,Encoding.UTF8,"application/json"));
            return (response.StatusCode,await response.Content.ReadAsByteArrayAsync());
        }
        var baseline=await Send(large,path);
        TraceAIAuto.Init(options=> {
            options.ConfigFile=AbsentConfig;
            if(route=="provider") options.Provider=provider;
            else if(route=="exporter") options.Exporter=exported;
            else { options.ApiKey="synthetic-api-key"; options.SecretKey="synthetic-secret-key"; options.BaseUrl=collector.Url; }
        });
        var captured=await Send(large,path);
        Assert.Equal(baseline.Status,captured.Status); Assert.Equal(baseline.Body,captured.Body);
        var later=await Send(Chat,"v1/chat/completions"); Assert.Equal(baseline.Body,later.Body);
        using var source=new ActivitySource("OpenAI.Discovery");
        using(var span=source.StartActivity("chat library")) { span!.SetTag("output.value","later library answer"); span.SetTag("gen_ai.usage.total_tokens",12L); }
        Assert.True(TraceAIAuto.Flush(15000));
        Assert.All(server.RequestBodies.Take(2),body=>Assert.Equal(Encoding.UTF8.GetBytes(large),body));
        Assert.NotNull(KnownSecrets.Snapshot());
        var spans=route=="otlp"?collector.Exports.SelectMany(Otlp.Spans).Select(span=>(Name:span.Name,Tags:span.Attributes)).ToArray():
            (route=="provider"?host:exported).Spans.Select(span=>(Name:span.DisplayName,Tags:span.TagObjects.ToDictionary(pair=>pair.Key,pair=>pair.Value!))).ToArray();
        var wire=spans.Where(span=>span.Tags.GetValueOrDefault("traceai.capture.layer") as string=="wire").ToArray();
        Assert.Equal(shape is "bulk" or "overflow"?1:2,wire.Length);
        Assert.Equal("chat gpt-4o-mini",wire[^1].Name); Assert.Equal("answer",wire[^1].Tags["output.value"]);
        Assert.All(wire,span=> { Assert.Equal("LLM",span.Tags["gen_ai.span.kind"]); Assert.Equal(12L,span.Tags["gen_ai.usage.total_tokens"]); Assert.Equal("r1",span.Tags["gen_ai.response.id"]); });
        if(shape=="chat-overflow") { Assert.False(wire[0].Tags.ContainsKey("output.value")); Assert.Equal(true,wire[0].Tags["traceai.wire.incomplete"]); }
        if(shape=="tools") Assert.Equal("answer",wire[0].Tags["output.value"]);
        Assert.Equal("later library answer",Assert.Single(spans,span=>span.Name=="chat library").Tags["output.value"]);
        if(route=="otlp") Assert.All(collector.Exports.SelectMany(Otlp.Spans),span=>Assert.Equal("checkout-api",span.Resource["service.name"]));
        else Assert.Equal("checkout-api",(route=="provider"?provider:exported.ParentProvider!).GetResource().Attributes.Single(pair=>pair.Key=="service.name").Value);
    }
    [Theory]
    [InlineData("provider",1100)] [InlineData("provider-exporter",1100)] [InlineData("exporter",1100)]
    [InlineData("provider",4000)] [InlineData("provider-exporter",4000)] [InlineData("exporter",4000)]
    public async Task HideImagesCannotDropLargeStructuredOutputSpans(string route,int count)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("OTEL_SDK_DISABLED",null),("FI_HIDE_INPUT_IMAGES","true"));
        string output=Records(count);
        byte[] body=Encoding.UTF8.GetBytes(Reply.Replace("\"answer\"",JsonSerializer.Serialize(output)));
        await using var server=new MockServer(body,"application/json") { UseContentLength=true };
        var host=new MemoryExporter(); var exported=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(new SimpleActivityExportProcessor(host)).Build();
        using var client=new HttpClient();
        async Task<(System.Net.HttpStatusCode Status,byte[] Body)> Send()
        {
            using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent(Chat,Encoding.UTF8,"application/json"));
            return (response.StatusCode,await response.Content.ReadAsByteArrayAsync());
        }
        var baseline=await Send();
        TraceAIAuto.Init(options=> {
            options.ConfigFile=AbsentConfig;
            if(route!="exporter") options.Provider=provider;
            if(route!="provider") options.Exporter=exported;
        });
        var captured=await Send(); Assert.Equal(baseline.Status,captured.Status); Assert.Equal(body,captured.Body); Assert.Equal(baseline.Body,captured.Body);
        Assert.True(TraceAIAuto.Flush(15000));
        var spans=new List<Activity> { Assert.Single(host.Spans) };
        if(route!="provider") spans.Add(Assert.Single(exported.Spans)); else Assert.Empty(exported.Spans);
        Assert.All(spans,span=> {
            Assert.Equal("chat gpt-4o-mini",span.DisplayName); Assert.Equal("LLM",span.GetTagItem("gen_ai.span.kind"));
            Assert.Equal(12L,span.GetTagItem("gen_ai.usage.total_tokens")); Assert.Equal("r1",span.GetTagItem("gen_ai.response.id"));
            Assert.Equal(count==1100?output:WirePrivacy.Redacted,span.GetTagItem("output.value"));
        });
        Assert.All(server.RequestBodies,request=>Assert.Equal(Encoding.UTF8.GetBytes(Chat),request));
    }
    [Theory]
    [InlineData("header")] [InlineData("query")] [InlineData("body-limit")] [InlineData("field")]
    public async Task OversizedDiscoveryInputsDoNotDisableTheRegistry(string shape)
    {
        using var content=shape=="field"?new MultipartFormDataContent { { new StringContent(new string('s',4097)),"client_secret" } }:
            (HttpContent)new StringContent(shape=="body-limit"?new string('x',1024):Chat,Encoding.UTF8,"application/json");
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://unrelated.invalid/orders"+(shape=="query"?"?key="+new string('s',4097):"")) { Content=content };
        request.Headers.TryAddWithoutValidation("Authorization","Bearer EARLY_HEADER_SECRET_9876");
        if(shape=="header") request.Headers.TryAddWithoutValidation("x-api-key",new string('s',4097));
        var config=new AutoConfig(new AutoOptions { Exporter=new MemoryExporter(),ConfigFile=AbsentConfig,WireMaxBodyBytes=512 });
        var before=await content.ReadAsByteArrayAsync();
        using var capture=new HttpCapture(config);
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        Assert.Same(content,request.Content); Assert.Equal(before,await request.Content.ReadAsByteArrayAsync());
        Assert.Contains("EARLY_HEADER_SECRET_9876",KnownSecrets.Snapshot());
        Assert.DoesNotContain(KnownSecrets.Snapshot(),secret=>secret.Length>KnownSecrets.MaxLength);
    }
    [Fact]
    public void BodyBudgetExhaustionKeepsAlreadyDiscoveredSecretsForLaterEchoes()
    {
        var request=new RequestData { Body=Encoding.UTF8.GetBytes("{\"client_secret\":\"EARLY_BODY_SECRET_9876\",\"items\":"+Records(4000)+"}") };
        KnownSecrets.Register(WirePrivacy.RequestSecrets(request));
        Assert.True(request.CredentialDiscoveryIncomplete);
        Assert.Equal("later <redacted>",WirePrivacy.ScrubString("later EARLY_BODY_SECRET_9876",KnownSecrets.Snapshot()));
    }
    [Fact]
    public void UnavailableRegistryKeepsResourceKeysAndNumericUsageWithoutLeakingValues()
    {
        var config=new AutoConfig(new AutoOptions { Exporter=new MemoryExporter(),ConfigFile=AbsentConfig });
        var privacy=new AutoProcessor(config);
        var resource=new Resource(new Dictionary<string,object> { ["service.name"]="PRIVATE_IDENTITY_9876",["project_name"]="PRIVATE_ROUTING_9876" });
        KnownSecrets.FailClosed();
        Assert.All(privacy.SanitizeResource(resource).Attributes,pair=>Assert.Equal(WirePrivacy.Redacted,pair.Value));
        Assert.Equal(2,privacy.SanitizeResource(resource).Attributes.Count());
        using var activity=new Activity("private name").Start();
        activity.SetTag("gen_ai.usage.total_tokens",12L); activity.SetTag("gen_ai.span.kind","LLM"); activity.SetTag("output.value","private answer"); activity.Stop();
        var batch=new Batch<Activity>([activity],1); var snapshot=privacy.PrepareExport(in batch,resource);
        var safe=Assert.Single(snapshot.Spans); Assert.Equal(12L,safe.GetTagItem("gen_ai.usage.total_tokens")); Assert.Null(safe.GetTagItem("output.value"));
        Assert.Equal(activity.TraceId,safe.TraceId); Assert.Equal(activity.Duration,safe.Duration);
        Assert.Equal(WirePrivacy.Redacted,snapshot.Resource.Attributes.Single(pair=>pair.Key=="service.name").Value);
    }
    [Fact]
    public void SanitizerFailureShellKeepsSafeSemanticsAndResourceIdentity()
    {
        using var original=new Activity("chat model").Start();
        original.SetTag("gen_ai.span.kind","LLM"); original.SetTag("gen_ai.operation.name","chat");
        original.SetTag("gen_ai.usage.total_tokens",12L); original.SetTag("output.value","private");
        original.SetTag("gen_ai.usage.extra.data","YQ=="); original.SetTag("gen_ai.usage.extra.mime_type","audio/wav");
        original.SetStatus(ActivityStatusCode.Error,"private"); original.Stop();
        var resource=new Resource(new Dictionary<string,object> { ["service.name"]="checkout-api",["project_name"]="routing",["broken"]="private" });
        object? Safe(string key,object value,string? operation)=>key is "output.value" or "broken"?throw new InvalidDataException():value;
        var snapshot=ExportSnapshot.Create([original],resource,Safe,text=>text);
        var safe=Assert.Single(snapshot.Spans);
        Assert.Equal(original.TraceId,safe.TraceId); Assert.Equal(original.Duration,safe.Duration); Assert.Equal(ActivityStatusCode.Error,safe.Status);
        Assert.Equal("LLM",safe.GetTagItem("gen_ai.span.kind")); Assert.Equal(12L,safe.GetTagItem("gen_ai.usage.total_tokens"));
        Assert.Null(safe.GetTagItem("output.value"));
        Assert.NotEqual("YQ==",safe.GetTagItem("gen_ai.usage.extra.data"));
        Assert.Equal("checkout-api",snapshot.Resource.Attributes.Single(pair=>pair.Key=="service.name").Value);
        Assert.Equal("routing",snapshot.Resource.Attributes.Single(pair=>pair.Key=="project_name").Value);
        Assert.DoesNotContain(snapshot.Resource.Attributes,pair=>Equals(pair.Value,"private"));
    }
}
