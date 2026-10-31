using System.Diagnostics;
using System.Text;
using System.Text.Json;
using FIInstrumentation;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class VerifierRegressionTests
{
    private const string Url="https://api.openai.com/v1/chat/completions";
    private static AutoOptions Options(BaseExporter<Activity>? exporter=null) => new() { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter };
    [Theory]
    [InlineData(10)] [InlineData(64)] [InlineData(65)] [InlineData(70)] [InlineData(1024)]
    public void DeepSerializedOutputFailsClosedForCredentialsAndMedia(int depth)
    {
        string payload=new('A',512);
        string content="{\"api_key\":\"DEEP_PRIVATE_KEY\",\"data\":\""+payload+"\"}";
        for(int i=0;i<depth;i++) content="{\"nested\":"+content+"}";
        var request=new RequestData { Url=Url,Body="{\"messages\":[]}"u8.ToArray() };
        var response=new ResponseData { Status=200,Body=JsonSerializer.SerializeToUtf8Bytes(new { choices=new[] { new { message=new { role="assistant",content } } } }) };
        var result=WireCore.Build(WireCore.Match("POST",Url)!,request,response);
        Assert.NotNull(result); Assert.True(result.Attributes.ContainsKey("output.value"));
        var attributes=WireCore.J(result.Attributes);
        Assert.DoesNotContain("DEEP_PRIVATE_KEY",attributes); Assert.DoesNotContain(payload,attributes);
        Assert.DoesNotContain("DEEP_PRIVATE_KEY",WirePrivacy.ScrubString(content));
        Assert.DoesNotContain(payload,WirePrivacy.ScrubString(content));
        if(depth==10) Assert.Contains("<omitted: 512 base64 chars>",attributes);
    }
    private sealed class LateProcessor(Action<Activity> enrich) : BaseProcessor<Activity>
    {
        public override void OnEnd(Activity activity) => enrich(activity);
    }
    [Theory]
    [InlineData(false,false)] [InlineData(false,true)] [InlineData(true,false)] [InlineData(true,true)]
    public async Task LateApplicationProcessorsCannotBypassWireExportPrivacy(bool builtIn,bool addEvent)
    {
        await using var server=new MockServer("{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"safe\"}}]}"u8.ToArray(),"application/json");
        var exporter=new MemoryExporter(); var options=Options(builtIn?null:exporter); options.BaseUrl=server.Url; options.TraceConfig=new TraceConfig { HideInputs=true };
        using var agent=new Agent(new AutoConfig(options));
        using var host=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).AddProcessor(new LateProcessor(activity=> {
            activity.SetTag("input.value","LATE_PRIVATE_INPUT");
            activity.SetTag("metadata",new[] { "ECHO_REQUEST_SECRET", "{\"api_key\":\"LATE_PRIVATE_KEY\"}" });
            activity.SetStatus(ActivityStatusCode.Error,"bad ECHO_REQUEST_SECRET");
            if(addEvent) activity.AddEvent(new ActivityEvent("late",tags:new ActivityTagsCollection { ["input.value"]="LATE_PRIVATE_EVENT" }));
        })).Build();
        using var client=new HttpClient(); client.DefaultRequestHeaders.Add("Authorization","Token ECHO_REQUEST_SECRET");
        using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
        await response.Content.ReadAsStringAsync(); Assert.True(agent.Flush(3000));
        if(builtIn)
        {
            var spans=server.Exports.SelectMany(Otlp.Spans).ToArray();
            var span=Assert.Single(spans); Assert.Equal(TraceConfig.RedactedValue,span.Attributes["input.value"]);
            Assert.DoesNotContain("ECHO_REQUEST_SECRET",WireCore.J(span.Attributes)); Assert.DoesNotContain("LATE_PRIVATE_KEY",WireCore.J(span.Attributes));
            Assert.DoesNotContain("LATE_PRIVATE_EVENT",Encoding.UTF8.GetString(Assert.Single(server.Exports)));
        }
        else
        {
            var span=Assert.Single(exporter.Spans); Assert.Equal(TraceConfig.RedactedValue,span.GetTagItem("input.value"));
            Assert.DoesNotContain("ECHO_REQUEST_SECRET",WireCore.J(span.GetTagItem("metadata"))); Assert.DoesNotContain("LATE_PRIVATE_KEY",WireCore.J(span.GetTagItem("metadata")));
            Assert.Equal("bad <redacted>",span.StatusDescription);
            if(addEvent) Assert.Equal(TraceConfig.RedactedValue,Assert.Single(span.Events).Tags.Single().Value);
        }
    }
    [Fact]
    public void NativeConsolidatedMessagesHonorTextOnlyHideFlags()
    {
        var exporter=new MemoryExporter(); var options=Options(exporter); options.TraceConfig=new TraceConfig { HideInputText=true,HideOutputText=true };
        using var agent=new Agent(new AutoConfig(options)); using var source=new ActivitySource("OpenAI.Consolidated.Privacy");
        using(var activity=source.StartActivity("chat"))
        {
            Assert.NotNull(activity);
            activity.SetTag("gen_ai.input.messages","[{\"role\":\"user\",\"content\":\"PRIVATE_INPUT\"}]");
            activity.SetTag("gen_ai.output.messages","[{\"role\":\"assistant\",\"content\":\"PRIVATE_OUTPUT\"}]");
        }
        Assert.True(agent.Flush(1000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal(TraceConfig.RedactedValue,span.GetTagItem("gen_ai.input.messages")); Assert.Equal(TraceConfig.RedactedValue,span.GetTagItem("gen_ai.output.messages"));
    }
    [Theory]
    [InlineData(false,false)] [InlineData(false,true)] [InlineData(true,false)] [InlineData(true,true)]
    public void NativeSuppressionIsRememberedWhenTheScopeEndsBeforeTheActivity(bool legacy,bool stopInside)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("OpenAI.Suppression.Privacy").Build();
        // Force another listener to record even when the OTel sampler respects its suppression scope.
        using var listener=new ActivityListener { ShouldListenTo=s=>s.Name=="OpenAI.Suppression.Privacy",Sample=(ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded };
        ActivitySource.AddActivityListener(listener); using var source=new ActivitySource("OpenAI.Suppression.Privacy"); Activity activity;
        using(legacy?(IDisposable)new SuppressTracing():SuppressInstrumentationScope.Begin())
        {
            activity=source.StartActivity("chat")!; Assert.NotNull(activity); activity.SetTag("input.value","SUPPRESSED_PRIVATE");
            if(stopInside) activity.Stop();
        }
        activity.Dispose(); Assert.True(agent.Flush(1000)); Assert.Empty(exporter.Spans);
        using(var visible=source.StartActivity("chat")) Assert.NotNull(visible);
        Assert.True(agent.Flush(1000)); Assert.Single(exporter.Spans);
    }
    [Fact]
    public async Task BorrowedOtlpExporterReceivesAndRefreshesTheAgentResource()
    {
        await using var server=new MockServer([],"application/json");
        using var exporter=new OtlpTraceExporter(new OtlpExporterOptions { Endpoint=new Uri(server.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf });
        for(int i=0;i<2;i++)
        {
            var options=Options(exporter); options.ProjectName="verify-project-"+i; options.ProjectVersionId="verify-version-"+i;
            using var agent=new Agent(new AutoConfig(options)); using(var activity=TraceAIAuto.Source.StartActivity("borrowed-"+i)) Assert.NotNull(activity);
            Assert.True(agent.Flush(3000));
        }
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(2,spans.Length);
        for(int i=0;i<2;i++)
        {
            var resource=spans[i].Resource; Assert.Equal("verify-project-"+i,resource["project_name"]); Assert.Equal("observe",resource["project_type"]);
            Assert.Equal("DEFAULT_PROJECT_VERSION_NAME",resource["project_version_name"]); Assert.Equal("verify-version-"+i,resource["project_version_id"]);
            Assert.Equal("[]",resource["eval_tags"]); Assert.Equal("{}",resource["metadata"]); Assert.Equal("fi",resource["semantic_convention"]);
        }
    }
    [Fact]
    public void BorrowedExporterKeepsItsExistingApplicationProviderAssociation()
    {
        var exporter=new MemoryExporter(); using var host=Sdk.CreateTracerProviderBuilder().AddSource("Host.Borrowed").AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var agent=new Agent(new AutoConfig(Options(exporter))))
        {
            using(var activity=TraceAIAuto.Source.StartActivity("agent")) Assert.NotNull(activity);
            Assert.True(agent.Flush(1000)); Assert.Same(host,exporter.ParentProvider);
        }
        using var source=new ActivitySource("Host.Borrowed"); using(var activity=source.StartActivity("host")) Assert.NotNull(activity);
        Assert.Equal(2,exporter.Spans.Count); Assert.Same(host,exporter.ParentProvider);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task DisabledNativeIntegrationDoesNotDeduplicateEnabledWireCapture(bool deny)
    {
        var exporter=new MemoryExporter(); var options=Options(exporter);
        if(deny) options.DisabledInstrumentations=["openai"]; else options.Instrumentations=["wire"];
        using var agent=new Agent(new AutoConfig(options));
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("OpenAI.WireOnly.Privacy").Build(); using var source=new ActivitySource("OpenAI.WireOnly.Privacy");
        await using var server=new MockServer("{}"u8.ToArray(),"application/json"); using var client=new HttpClient();
        using(var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"))) await response.Content.ReadAsStringAsync();
        ActivitySpanId parent;
        using(var activity=source.StartActivity("chat"))
        {
            Assert.NotNull(activity); Assert.Null(activity.GetTagItem("gen_ai.span.kind")); parent=activity.SpanId;
            using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json")); await response.Content.ReadAsStringAsync();
        }
        Assert.True(agent.Flush(1000)); Assert.Equal(2,exporter.Spans.Count); Assert.Contains(exporter.Spans,s=>s.ParentSpanId==parent);
    }
    [Theory]
    [InlineData(false,false)] [InlineData(false,true)] [InlineData(true,false)] [InlineData(true,true)]
    public void AbsentAndExplicitlyEmptyBodiesDoNotBecomeUnavailable(bool known,bool explicitEmpty)
    {
        var exporter=new MemoryExporter(); var options=Options(exporter); options.WireCapture=false;
        using var agent=new Agent(new AutoConfig(options)); using var capture=new HttpCapture(new AutoConfig(Options(exporter)));
        string url=known?Url:"https://proxy.invalid/v1/chat/completions";
        var requestData=new RequestData { Url=url,Body=explicitEmpty?[]:null };
        Assert.Null(WireCore.Build(WireCore.Match("POST",url)!,requestData,new ResponseData { Status=200 })); Assert.False(requestData.BodyUnavailable);
        using var request=new HttpRequestMessage(HttpMethod.Post,url) { Content=explicitEmpty?new ByteArrayContent([]):null };
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        using var response=new HttpResponseMessage(System.Net.HttpStatusCode.OK) { Content=new StringContent("{}",Encoding.UTF8,"application/json") };
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Stop",new { Request=request,Response=response }));
        response.Content.CopyTo(Stream.Null,null,CancellationToken.None); Assert.True(agent.Flush(1000)); Assert.Empty(exporter.Spans);
    }
}
