using System.Diagnostics;
using System.Text;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundSixBlockerTests : InitIsolation
{
    private sealed class EnvironmentScope : IDisposable
    {
        private readonly Dictionary<string,string?> _previous=new();
        internal EnvironmentScope(params (string Name,string? Value)[] values)
        {
            foreach(var (name,value) in values)
            {
                _previous[name]=Environment.GetEnvironmentVariable(name);
                Environment.SetEnvironmentVariable(name,value);
            }
        }
        public void Dispose() { foreach(var (name,value) in _previous) Environment.SetEnvironmentVariable(name,value); }
    }
    private static string AbsentConfig=>Path.Combine(WireFixtureTests.Repo,"csharp/absent.toml");
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void IgnoredFiCredentialSourcesAreRedactedThroughPublicInitAndAfterRegistryChurn(bool completeExplicitPair)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY","SELECTED_ENV_API_KEY_9876"),("FI_SECRET_KEY","SELECTED_ENV_SECRET_KEY_9876"),("OTEL_SDK_DISABLED",null));
        string path=Path.Combine(Path.GetTempPath(),"traceai-s6-credentials-"+Guid.NewGuid()+".toml");
        try
        {
            File.WriteAllText(path,"api_key = \"IGNORED_FILE_API_KEY_9876\"\nsecret_key = \"IGNORED_FILE_SECRET_KEY_9876\"\n");
            var exporter=new MemoryExporter();
            TraceAIAuto.Init(options=> {
                options.ConfigFile=path; options.ApiKey="EXPLICIT_API_KEY_9876";
                if(completeExplicitPair) options.SecretKey="EXPLICIT_SECRET_KEY_9876";
                options.Exporter=exporter;
            });
            for(int i=0;i<KnownSecrets.Capacity+1;i++) TraceAIAuto.RegisterSecret("CHURN_VALUE_"+i);
            using(var span=TraceAIAuto.Source.StartActivity("later"))
            {
                Assert.NotNull(span);
                span.SetTag("later.echo","EXPLICIT_API_KEY_9876 IGNORED_FILE_API_KEY_9876 IGNORED_FILE_SECRET_KEY_9876 SELECTED_ENV_API_KEY_9876 SELECTED_ENV_SECRET_KEY_9876");
            }
            Assert.True(TraceAIAuto.Flush());
            Assert.Equal("<redacted> <redacted> <redacted> <redacted> <redacted>",Assert.Single(exporter.Spans).GetTagItem("later.echo"));
            string doctor=TraceAIAuto.Doctor();
            Assert.DoesNotContain("API_KEY_9876",doctor); Assert.DoesNotContain("SECRET_KEY_9876",doctor);
        }
        finally { File.Delete(path); }
    }
    [Fact]
    public void ProjectRoutingPreservesDetectedApplicationResourceIdentity()
    {
        using var environment=new EnvironmentScope(("OTEL_SERVICE_NAME","checkout-api"),
            ("OTEL_RESOURCE_ATTRIBUTES","service.name=resource-name,service.version=9.8.7,service.instance.id=checkout-instance"),("OTEL_SDK_DISABLED",null));
        var exporter=new MemoryExporter();
        TraceAIAuto.Init(options=> { options.ApiKey="identity-api-key"; options.SecretKey="identity-secret-key"; options.ProjectName="routing-project"; options.ConfigFile=AbsentConfig; options.Exporter=exporter; });
        using(var activity=TraceAIAuto.Source.StartActivity("identity")) Assert.NotNull(activity);
        Assert.True(TraceAIAuto.Flush()); Assert.Single(exporter.Spans);
        var attributes=exporter.ParentProvider!.GetResource().Attributes.ToDictionary(pair=>pair.Key,pair=>pair.Value);
        Assert.Equal("checkout-api",attributes["service.name"]); Assert.Equal("9.8.7",attributes["service.version"]);
        Assert.Equal("checkout-instance",attributes["service.instance.id"]); Assert.Equal("routing-project",attributes["project_name"]);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void SuppliedExporterActivatesWithoutFiCredentialsAndDisabledStillWins(bool disabled)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("OTEL_SDK_DISABLED",disabled?"true":null));
        var exporter=new MemoryExporter();
        TraceAIAuto.Init(options=> { options.Exporter=exporter; options.ConfigFile=AbsentConfig; });
        using(var activity=TraceAIAuto.Source.StartActivity("neutral"))
        {
            Assert.Equal(!disabled,activity!=null);
            activity?.SetTag("provider.api_key","UNRELATED_MARKED_KEY_9876");
        }
        Assert.True(TraceAIAuto.Flush());
        if(disabled) Assert.Empty(exporter.Spans);
        else Assert.Equal("__REDACTED__",Assert.Single(exporter.Spans).GetTagItem("provider.api_key"));
    }
    [Fact]
    public void NeutralExporterDoesNotResolveAnUnusedFiCollectorUrl()
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("FI_BASE_URL","not a collector URL"),("OTEL_SDK_DISABLED",null));
        var exporter=new MemoryExporter();
        TraceAIAuto.Init(options=> { options.Exporter=exporter; options.ConfigFile=AbsentConfig; });
        using(var activity=TraceAIAuto.Source.StartActivity("neutral")) Assert.NotNull(activity);
        Assert.True(TraceAIAuto.Flush()); Assert.Single(exporter.Spans);
    }
    [Theory]
    [InlineData("provider",false)] [InlineData("provider",true)]
    [InlineData("provider-exporter",false)] [InlineData("provider-exporter",true)]
    [InlineData("exporter",false)] [InlineData("exporter",true)]
    public async Task InvalidFiProjectTypeDoesNotDisableNeutralActivation(string route,bool explicitOption)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("FI_PROJECT_TYPE",explicitOption?null:"bogus"),("OTEL_SDK_DISABLED",null));
        var hostExporter=new MemoryExporter(); var agentExporter=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string,object> { ["service.name"]="checkout",["project_type"]="host-routing" }))
            .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        TraceAIAuto.Init(options=> {
            options.ConfigFile=AbsentConfig;
            if(route!="exporter") options.Provider=provider;
            if(route!="provider") options.Exporter=agentExporter;
            if(explicitOption) options.ProjectType="bogus";
        });
        using var doctor=System.Text.Json.JsonDocument.Parse(TraceAIAuto.Doctor());
        Assert.True(doctor.RootElement.GetProperty("enabled").GetBoolean());
        Assert.Equal("observe",doctor.RootElement.GetProperty("configuration").GetProperty("FI_PROJECT_TYPE").GetString());
        await using var server=new MockServer("{\"choices\":[{\"message\":{\"content\":\"answer\"}}]}"u8.ToArray(),"application/json");
        using var client=new HttpClient();
        using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"model\":\"m\",\"messages\":[]}",Encoding.UTF8,"application/json"));
        Assert.Contains("answer",await response.Content.ReadAsStringAsync());
        Assert.True(TraceAIAuto.Flush());
        Assert.Equal("answer",Assert.Single(hostExporter.Spans).GetTagItem("output.value"));
        if(route=="provider") Assert.Empty(agentExporter.Spans);
        else Assert.Equal("answer",Assert.Single(agentExporter.Spans).GetTagItem("output.value"));
        if(route=="exporter") Assert.Equal("observe",agentExporter.ParentProvider!.GetResource().Attributes.Single(pair=>pair.Key=="project_type").Value);
        Assert.Equal("host-routing",provider.GetResource().Attributes.Single(pair=>pair.Key=="project_type").Value);
    }
    [Fact]
    public void InvalidFiProjectTypeStillRejectsDefaultCollectorConfiguration()
    {
        using var environment=new EnvironmentScope(("FI_PROJECT_TYPE","bogus"));
        Assert.Throws<ArgumentException>(()=>new AutoConfig(new AutoOptions { ApiKey="synthetic-api",SecretKey="synthetic-secret",BaseUrl="http://127.0.0.1:1",ConfigFile=AbsentConfig }));
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task SuppliedProviderActivatesWithoutFiCredentialsAndRemainsApplicationOwned(bool disabled)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("OTEL_SDK_DISABLED",null));
        var exporter=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name,"Host.Neutral")
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string,object> { ["service.name"]="checkout",["service.namespace"]="payments",["cloud.region"]="test-region" }))
            .AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using var disabledEnvironment=new EnvironmentScope(("OTEL_SDK_DISABLED",disabled?"true":null));
        TraceAIAuto.Init(options=> { options.Provider=provider; options.ConfigFile=AbsentConfig; options.ProjectName="routing-only"; });
        await using var server=new MockServer("{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"answer\"}}]}"u8.ToArray(),"application/json");
        using var client=new HttpClient();
        using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"model\":\"m\",\"messages\":[]}",Encoding.UTF8,"application/json"));
        Assert.Contains("answer",await response.Content.ReadAsStringAsync());
        Assert.True(TraceAIAuto.Flush());
        if(disabled) Assert.Empty(exporter.Spans); else Assert.Equal("answer",Assert.Single(exporter.Spans).GetTagItem("output.value"));
        var resource=provider.GetResource().Attributes.ToDictionary(pair=>pair.Key,pair=>pair.Value);
        Assert.Equal("checkout",resource["service.name"]); Assert.Equal("payments",resource["service.namespace"]); Assert.Equal("test-region",resource["cloud.region"]);
        Assert.False(resource.ContainsKey("project_name"));
        TraceAIAuto.Shutdown();
        using var source=new ActivitySource("Host.Neutral");
        using(var activity=source.StartActivity("after-shutdown")) Assert.NotNull(activity);
        Assert.Contains(exporter.Spans,span=>span.DisplayName=="after-shutdown");
    }
    public static IEnumerable<object[]> WirePrivacyRoutes =>
        from route in new[] { "provider", "provider-exporter", "exporter" }
        from source in new[] { "environment", "options", "file", "default" }
        from stream in new[] { false, true }
        select new object[] { route,source,stream };
    [Theory]
    [MemberData(nameof(WirePrivacyRoutes))]
    public async Task WireHideControlsApplyBeforeActivityCreationOnEveryDestination(string route,string source,bool stream)
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("OTEL_SDK_DISABLED",null),
            ("FI_HIDE_INPUTS",source=="environment"?"true":null),("FI_HIDE_OUTPUTS",source=="environment"?"true":null));
        const string input="{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"private question\"}]}";
        const string answer="private answer";
        byte[] body=Encoding.UTF8.GetBytes(stream?
            "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\",\"content\":\"private answer\"}}]}\n\ndata: [DONE]\n\n":
            "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"private answer\"}}]}");
        await using var server=new MockServer(body,stream?"text/event-stream":"application/json");
        var hostExporter=new MemoryExporter(); var agentExporter=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name,"OpenAI.HideControl")
            .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        var starts=new List<Dictionary<string,object?>>();
        using var listener=new ActivityListener {
            ShouldListenTo=activitySource=>activitySource.Name==TraceAIAuto.Source.Name,
            ActivityStarted=activity=>starts.Add(activity.TagObjects.ToDictionary(pair=>pair.Key,pair=>pair.Value))
        };
        ActivitySource.AddActivityListener(listener);
        using var client=new HttpClient();
        async Task<(System.Net.HttpStatusCode Status,byte[] Body)> Send()
        {
            using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent(input,Encoding.UTF8,"application/json"));
            return (response.StatusCode,await response.Content.ReadAsByteArrayAsync());
        }
        var baseline=await Send(); Assert.Empty(hostExporter.Spans);
        string path=Path.Combine(Path.GetTempPath(),"traceai-wire-hide-"+Guid.NewGuid()+".toml");
        try
        {
            if(source=="file") File.WriteAllText(path,"hide_inputs = true\nhide_outputs = true\n");
            TraceAIAuto.Init(options=> {
                options.ConfigFile=source=="file"?path:AbsentConfig;
                if(route!="exporter") options.Provider=provider;
                if(route!="provider") options.Exporter=agentExporter;
                if(source=="options") options.TraceConfig=new FIInstrumentation.TraceConfig { HideInputs=true,HideOutputs=true };
            });
            var captured=await Send();
            Assert.Equal(baseline.Status,captured.Status); Assert.Equal(body,baseline.Body); Assert.Equal(baseline.Body,captured.Body);
            Assert.Equal(2,server.RequestBodies.Count);
            Assert.All(server.RequestBodies,request=>Assert.Equal(Encoding.UTF8.GetBytes(input),request));
            Assert.True(TraceAIAuto.Flush());
            var host=Assert.Single(hostExporter.Spans);
            var started=Assert.Single(starts);
            var expected=new Dictionary<string,string> {
                ["input.value"]=input,["gen_ai.input.messages.0.message.content"]="private question",
                ["output.value"]=answer,["gen_ai.output.messages.0.message.content"]=answer
            };
            foreach(var pair in expected)
            {
                string value=source=="default"?pair.Value:FIInstrumentation.TraceConfig.RedactedValue;
                Assert.Equal(value,started[pair.Key]); Assert.Equal(value,host.GetTagItem(pair.Key));
                if(route!="provider") Assert.Equal(value,Assert.Single(agentExporter.Spans).GetTagItem(pair.Key));
            }
            if(route=="provider") Assert.Empty(agentExporter.Spans);
            // Agent-created wire content is masked at creation; host/library content
            // must still keep its original view for the host-owned destination (S8).
            using var library=new ActivitySource("OpenAI.HideControl");
            using(var span=library.StartActivity("chat host")) span!.SetTag("output.value",answer);
            Assert.True(TraceAIAuto.Flush());
            Assert.Equal(answer,Assert.Single(hostExporter.Spans,span=>span.DisplayName=="chat host").GetTagItem("output.value"));
            if(route!="provider") Assert.Equal(source=="default"?answer:FIInstrumentation.TraceConfig.RedactedValue,
                Assert.Single(agentExporter.Spans,span=>span.DisplayName=="chat host").GetTagItem("output.value"));
        }
        finally { TraceAIAuto.Shutdown(); File.Delete(path); }
    }
    [Fact]
    public async Task SuppliedProviderAndExporterPreserveEveryHostResourceFieldOnBothViews()
    {
        using var environment=new EnvironmentScope(("FI_API_KEY",null),("FI_SECRET_KEY",null),("OTEL_SDK_DISABLED",null));
        await using var collector=new MockServer([],"application/json");
        var hostExporter=new MemoryExporter();
        var resource=new Dictionary<string,object> { ["service.name"]="checkout",["service.namespace"]="payments",["deployment.environment.name"]="staging",
            ["cloud.region"]="test-region",["metadata"]="application metadata",["eval_tags"]="application tags",["semantic_convention"]="application schema" };
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(resource))
            .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        using var exporter=new OtlpTraceExporter(new() { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf });
        TraceAIAuto.Init(options=> { options.Provider=provider; options.Exporter=exporter; options.ConfigFile=AbsentConfig; options.ProjectName="routing-only"; });
        using(var activity=TraceAIAuto.Source.StartActivity("host-export")) activity!.SetTag("output.value","answer");
        Assert.True(TraceAIAuto.Flush());
        var span=Assert.Single(Otlp.Spans(Assert.Single(collector.Exports)));
        foreach(var pair in resource) Assert.Equal(pair.Value,span.Resource[pair.Key]);
        Assert.Equal("routing-only",span.Resource["project_name"]);
        Assert.DoesNotContain(provider.GetResource().Attributes,pair=>pair.Key=="project_name");
        TraceAIAuto.Shutdown();
        using(var activity=TraceAIAuto.Source.StartActivity("host-still-running")) Assert.NotNull(activity);
        Assert.Equal(2,hostExporter.Spans.Count); Assert.Single(collector.Exports);
    }
    [Theory]
    [InlineData(false,null)] [InlineData(true,null)]
    [InlineData(false,8)] [InlineData(true,8)]
    [InlineData(false,0)] [InlineData(true,0)]
    public async Task ActualOtlpPrioritizesOutcomeUsageIdentityProvenanceAndDegradationWithoutLosingDroppedCounts(bool compatibility,int? limit)
    {
        using var environment=new EnvironmentScope(("OTEL_ATTRIBUTE_COUNT_LIMIT",limit?.ToString()),("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT",null));
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(new AutoConfig(new AutoOptions { ApiKey="limit-api-key",SecretKey="limit-secret-key",ConfigFile=AbsentConfig }));
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using var original=TraceAIAuto.Source.StartActivity("chat")!;
        for(int i=0;i<140;i++) original.SetTag(((i%3) switch {
            0=>"input.messages.",1=>"traceai.extra.",_=>"gen_ai.usage.extra." })+i+".content","detail");
        original.SetTag("gen_ai.operation.name","chat"); original.SetTag("gen_ai.provider.name","openai_compatible");
        original.SetTag("traceai.capture.layer","wire"); original.SetTag("gen_ai.usage.input_tokens",11L); original.SetTag("gen_ai.usage.output_tokens",3L);
        original.SetTag("output.value","primary answer"); original.SetTag("traceai.wire.incomplete",true); original.SetTag("error.type","TimeoutException");
        original.SetStatus(ActivityStatusCode.Error,"partial"); original.Stop();
        var span=Assert.Single(Otlp.Spans(Assert.Single(collector.Exports)));
        int effective=limit??128;
        Assert.Equal(effective,span.Attributes.Count);
        // A7 removes candidates before SDK serialization. SDK eviction is distinct
        // from projected content loss and must not be fabricated from that count.
        Assert.Equal(0UL,span.DroppedAttributes);
        if(effective>0)
        {
            Assert.Equal("TimeoutException",span.Attributes["error.type"]); Assert.Equal(true,span.Attributes["traceai.wire.incomplete"]);
            Assert.Equal("LLM",span.Attributes["gen_ai.span.kind"]);
            Assert.Equal("chat",span.Attributes["gen_ai.operation.name"]); Assert.Equal("openai_compatible",span.Attributes["gen_ai.provider.name"]);
        }
        if(effective>=128)
        {
            Assert.Equal(14L,span.Attributes["gen_ai.usage.total_tokens"]); Assert.Equal("wire",span.Attributes["traceai.capture.layer"]);
            Assert.Equal("primary answer",span.Attributes["output.value"]);
        }
        if(effective==8)
        {
            Assert.Equal(48L,span.Attributes["traceai.capture.dropped_content_count"]);
            Assert.False(span.Attributes.ContainsKey("output.value"));
        }
        Assert.Equal("primary answer",original.GetTagItem("output.value")); Assert.Null(original.GetTagItem("gen_ai.usage.total_tokens"));
        Assert.True(original.TagObjects.Count()>140);
    }
    [Fact]
    public void AttributeProjectionIsIndependentOfInsertionOrder()
    {
        var tags=new[] { "noise","input.value","gen_ai.usage.total_tokens","output.value","error.type","traceai.capture.layer","traceai.wire.incomplete" }
            .Select(key=>new KeyValuePair<string,object?>(key,"value")).ToArray();
        Assert.Equal(AttributeProjection.Order(tags).Select(pair=>pair.Key),AttributeProjection.Order(tags.Reverse()).Select(pair=>pair.Key));
    }
    [Fact]
    public async Task RealHttpClientKeepsRoleChunkAndFirstOutputEventMilestonesSeparate()
    {
        byte[] role="data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\r\n\r\n"u8.ToArray();
        byte[] answer="data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\r\n\r\ndata: [DONE]\r\n\r\n"u8.ToArray();
        var release=new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var server=new MockServer([],"text/event-stream") { ResponseWriter=async stream=> {
            await stream.WriteAsync(role); await stream.FlushAsync();
            await release.Task.WaitAsync(TimeSpan.FromSeconds(10));
            await stream.WriteAsync(answer); await stream.FlushAsync();
        } };
        var exporter=new MemoryExporter();
        using var agent=new Agent(new AutoConfig(new AutoOptions { ApiKey="stream-api-key",SecretKey="stream-secret-key",Exporter=exporter,ConfigFile=AbsentConfig }));
        using var client=new HttpClient();
        using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") { Content=new StringContent("{\"model\":\"m\",\"messages\":[]}",Encoding.UTF8,"application/json") };
        try
        {
            using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
            using var stream=await response.Content.ReadAsStreamAsync();
            var first=new byte[role.Length]; await stream.ReadExactlyAsync(first); Assert.Equal(role,first);
            release.TrySetResult();
            using var remainder=new MemoryStream(); await stream.CopyToAsync(remainder); Assert.Equal(answer,remainder.ToArray());
            Assert.True(agent.Flush(3000));
            var span=Assert.Single(exporter.Spans);
            Assert.Equal("answer",span.GetTagItem("output.value"));
            double chunk=Assert.IsType<double>(span.GetTagItem("gen_ai.response.time_to_first_chunk"));
            double text=Assert.IsType<double>(span.GetTagItem("gen_ai.response.time_to_first_text"));
            Assert.True(text>chunk,$"chunk={chunk}, text={text}");
        }
        finally { release.TrySetResult(); }
    }
    [Fact]
    public void ArrivalBookkeepingIsBoundedAndDoesNotGuessBeyondItsBudget()
    {
        var response=new ResponseData();
        for(int i=0;i<StreamMilestones.MaxArrivals+10;i++) response.ObserveChunk(1,i/1000.0);
        Assert.Equal(StreamMilestones.MaxArrivals,response.Arrivals.Count);
        Assert.Equal(0,response.FirstChunkSeconds); Assert.Null(response.ArrivalAt(StreamMilestones.MaxArrivals+1));
    }
}
