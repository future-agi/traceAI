using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFourPrivacyTests : InitIsolation
{
    private static AutoOptions Options(BaseExporter<Activity>? exporter=null,TraceConfig? mask=null)=>new() {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,TraceConfig=mask,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") };
    private sealed class SecondaryContext() : System.Runtime.Loader.AssemblyLoadContext(isCollectible:true)
    {
        protected override System.Reflection.Assembly? Load(System.Reflection.AssemblyName name)=>
            Default.Assemblies.FirstOrDefault(a=>a.GetName().Name==name.Name);
    }
    [Fact]
    public async Task InitIsOnceEvenAcrossManualAndIsolatedRuntimeLoadContexts()
    {
        await using var collector=new MockServer([],"application/json");
        string[] keys=["FI_API_KEY","FI_SECRET_KEY","FI_BASE_URL"];
        var old=keys.ToDictionary(k=>k,Environment.GetEnvironmentVariable);
        var context=new SecondaryContext(); ActivitySource? secondSource=null; Type? entry=null;
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY","explicit-api-key"); Environment.SetEnvironmentVariable("FI_SECRET_KEY","explicit-secret-key");
            Environment.SetEnvironmentVariable("FI_BASE_URL",collector.Url);
            var memory=new MemoryExporter(); TraceAIAuto.Init(o=> { o.ApiKey="explicit-api-key"; o.SecretKey="explicit-secret-key"; o.Exporter=memory; });
            var assembly=context.LoadFromAssemblyPath(typeof(TraceAIAuto).Assembly.Location);
            entry=assembly.GetType("TraceAI.AutoInstrumentation.TraceAIAuto")!;
            entry.GetMethod("Init")!.Invoke(null,new object?[] { null });
            secondSource=(ActivitySource)entry.GetField("Source",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.NonPublic)!.GetValue(null)!;
            using(var span=secondSource.StartActivity("cross context once")) Assert.NotNull(span);
            entry.GetMethod("Shutdown")!.Invoke(null,new object[] { 3000 });
            Assert.True(TraceAIAuto.Flush(3000)); Assert.Single(memory.Spans); Assert.Empty(collector.Exports);
        }
        finally
        {
            entry?.GetMethod("Shutdown")?.Invoke(null,new object[] { 3000 }); secondSource?.Dispose();
            TraceAIAuto.Shutdown(); context.Unload();
            foreach(var pair in old) Environment.SetEnvironmentVariable(pair.Key,pair.Value);
        }
    }
    private sealed class MutatingOtlp(OtlpExporterOptions options,Action mutate) : OtlpTraceExporter(options)
    {
        public override ExportResult Export(in Batch<Activity> batch) { mutate(); return base.Export(in batch); }
    }
    [Fact]
    public async Task ConcurrentApplicationEnrichmentCannotMutateTheActualOtlpSnapshot()
    {
        await using var collector=new MockServer([],"application/json");
        Activity? original=null; string[] notes=["safe"];
        using var exporter=new MutatingOtlp(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },()=> {
            original!.SetTag("input.value","LATE_PRIVATE_INPUT explicit-api-key"); notes[0]="explicit-secret-key";
        });
        using var agent=new Agent(new AutoConfig(Options(exporter,new TraceConfig { HideInputs=true })));
        using(var activity=TraceAIAuto.Source.StartActivity("snapshot",ActivityKind.Client))
        {
            original=activity!; Assert.NotNull(original); original.SetTag("input.value","ORIGINAL_PRIVATE_INPUT"); original.SetTag("notes",notes);
        }
        var trace=original.TraceId; var id=original.SpanId; var start=original.StartTimeUtc; var duration=original.Duration;
        Assert.True(agent.Flush(3000));
        var bytes=Assert.Single(collector.Exports); var span=Assert.Single(Otlp.Spans(bytes));
        Assert.Equal(TraceConfig.RedactedValue,span.Attributes["input.value"]);
        string body=Encoding.UTF8.GetString(bytes);
        foreach(var secret in new[] { "explicit-api-key","explicit-secret-key","LATE_PRIVATE_INPUT","ORIGINAL_PRIVATE_INPUT" }) Assert.DoesNotContain(secret,body);
        Assert.Equal("LATE_PRIVATE_INPUT explicit-api-key",original.GetTagItem("input.value"));
        Assert.Same(notes,original.GetTagItem("notes")); Assert.Equal("explicit-secret-key",notes[0]);
        Assert.Equal(id.ToHexString().ToUpperInvariant(),span.Id); Assert.Equal(trace,original.TraceId); Assert.Equal(start,original.StartTimeUtc); Assert.Equal(duration,original.Duration);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task AllExportSurfacesAreSanitizedWithoutDroppingSpans(bool compatibility)
    {
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(new AutoConfig(Options(mask:new TraceConfig { HideInputs=true,HideOutputs=true })));
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions {
            Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("Round4.Scope.explicit-api-key","explicit-secret-key",new Dictionary<string,object?> {
            ["password"]="PRIVATE_SCOPE_PASSWORD",["input.value"]="PRIVATE_SCOPE_INPUT",["safe"]="retained" });
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string,object> {
                ["authorization"]="PRIVATE_RESOURCE_PASSWORD",["input.custom"]="PRIVATE_RESOURCE_INPUT",["note"]="explicit-secret-key",["safe"]="retained" }))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        var linkContext=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"vendor=explicit-secret-key",true);
        using(var span=source.StartActivity("display explicit-api-key",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(linkContext,new ActivityTagsCollection { ["output.custom"]="PRIVATE_LINK_OUTPUT",["api-key"]="PRIVATE_LINK_KEY",["safe"]="retained" }) }))
        {
            Assert.NotNull(span); span.TraceStateString="vendor=explicit-api-key";
            span.SetTag("input.custom",new[] { "PRIVATE_INPUT" }); span.SetTag("output.custom",new[] { 1,2 });
            span.SetTag("notes",new[] { "explicit-api-key","https://host/file?sig=PRIVATE_SIGNATURE","data:audio/wav;base64,QUJD" });
            span.SetTag("data",new string('A',512));
            span.SetStatus(ActivityStatusCode.Error,"explicit-secret-key https://host/file?token=PRIVATE_TOKEN");
            span.AddEvent(new ActivityEvent("event explicit-api-key",tags:new ActivityTagsCollection {
                ["input.value"]="PRIVATE_EVENT_INPUT",["exception.message"]="explicit-secret-key",["exception.stacktrace"]="https://host/file?key=PRIVATE_STACK_KEY" }));
        }
        var bytes=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(bytes);
        foreach(var secret in new[] { "explicit-api-key","explicit-secret-key","PRIVATE_",new string('A',512),"QUJD" }) Assert.DoesNotContain(secret,actual);
        var decoded=Assert.Single(Otlp.Spans(bytes));
        Assert.Equal(TraceConfig.RedactedValue,decoded.Attributes["input.custom"]); Assert.Equal(TraceConfig.RedactedValue,decoded.Attributes["output.custom"]);
        Assert.NotEmpty(Otlp.Extras(bytes,11)); Assert.NotEmpty(Otlp.Extras(bytes,13)); Assert.Contains("retained",actual);
    }
    [Theory]
    [InlineData("LLM",false,true)] [InlineData("LLM",true,true)] [InlineData("AGENT",false,true)] [InlineData("AGENT",true,true)]
    [InlineData("LLM",false,false)] [InlineData("LLM",true,false)] [InlineData("AGENT",false,false)] [InlineData("AGENT",true,false)]
    public async Task ProviderCredentialEchoesAreScrubbedOnLibraryAndFrameworkSpansEvenWhenWireIsDeduplicated(string kind,bool detached,bool wire)
    {
        const string credential="PROVIDER_SECRET_R4_KEY";
        string payload="{\"error\":{\"message\":\"bad "+credential+"\"}}";
        await using var server=new MockServer(Encoding.UTF8.GetBytes(payload),"application/json") { StatusCode=401 };
        var options=Options(); options.BaseUrl=server.Url; options.WireCapture=wire; options.Metadata=new() { ["echo"]=credential };
        using var agent=new Agent(new AutoConfig(options));
        using var source=new ActivitySource("OpenAI.Round4.LibrarySecrets",credential);
        using var nativeSource=new ActivitySource("Native.Round4.SecretClient");
        using var listener=new ActivityListener { ShouldListenTo=s=>s.Name==nativeSource.Name,Sample=(ref ActivityCreationOptions<ActivityContext> _)=>ActivitySamplingResult.AllDataAndRecorded };
        ActivitySource.AddActivityListener(listener);
        using(var model=source.StartActivity("wrapper",ActivityKind.Client,default(ActivityContext),new ActivityTagsCollection { ["gen_ai.span.kind"]=kind }))
        {
            Assert.NotNull(model); Activity? native=null; var ambient=Activity.Current;
            try
            {
                if(detached) { Activity.Current=null; native=nativeSource.StartActivity("client",ActivityKind.Client,model.Context); Assert.NotNull(native); }
                using var client=new HttpClient(); client.DefaultRequestHeaders.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",credential);
                using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
                Assert.Equal(payload,await response.Content.ReadAsStringAsync());
            }
            finally { native?.Dispose(); Activity.Current=ambient; }
            model.SetStatus(ActivityStatusCode.Error,"bad "+credential);
            model.AddEvent(new ActivityEvent("provider failure",tags:new ActivityTagsCollection { ["exception.message"]=new[] { credential } }));
        }
        Assert.True(agent.Flush(3000));
        foreach(var bytes in server.Exports) Assert.DoesNotContain(credential,Encoding.UTF8.GetString(bytes));
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(kind=="LLM"||!wire?1:2,spans.Length);
        Assert.Single(spans,s=>s.Attributes.GetValueOrDefault("traceai.capture.layer") as string=="library");
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task NonFiniteScalarsAndArraysPreserveEverySpanAndFiniteValue(bool compatibility)
    {
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(new AutoConfig(Options()));
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        foreach(double value in new[] { 1d,double.NaN,double.PositiveInfinity,double.NegativeInfinity })
            using(var span=TraceAIAuto.Source.StartActivity("nonfinite")) { span!.SetTag("value",value); span.SetTag("array",new[] { value,2d }); }
        var spans=collector.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(4,spans.Length);
        Assert.Equal(1d,Convert.ToDouble(spans[0].Attributes["value"]));
        Assert.Equal("NaN",spans[1].Attributes["value"]); Assert.Equal("Infinity",spans[2].Attributes["value"]); Assert.Equal("-Infinity",spans[3].Attributes["value"]);
        Assert.Equal("[1,2]",WireCore.J(spans[0].Attributes["array"]));
        Assert.Equal("[\"NaN\",2]",WireCore.J(spans[1].Attributes["array"]));
        Assert.Equal("[\"Infinity\",2]",WireCore.J(spans[2].Attributes["array"]));
        Assert.Equal("[\"-Infinity\",2]",WireCore.J(spans[3].Attributes["array"]));
    }
    [Theory]
    [InlineData("scalar")] [InlineData("strings")] [InlineData("objects")]
    public void ImageHidingIsElementWiseForEveryArrayShape(string shape)
    {
        const string value="[{\"type\":\"image_url\",\"image_url\":{\"url\":\"https://private.example/patient.png\"}}]";
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter,new TraceConfig { HideInputImages=true })));
        using(var span=TraceAIAuto.Source.StartActivity("images")) span!.SetTag("gen_ai.input.messages",shape=="scalar"?value:shape=="strings"?new[] { value }:new object[] { value });
        Assert.True(agent.Flush(3000)); string actual=WireCore.J(Assert.Single(exporter.Spans).GetTagItem("gen_ai.input.messages"));
        Assert.DoesNotContain("patient.png",actual); Assert.Contains(TraceConfig.RedactedValue,actual);
    }
    [Theory]
    [InlineData("fileData","mimeType","fileUri")] [InlineData("file_data","mime_type","file_uri")]
    public async Task GeminiFileBackedImageLocationsNeverReachActualOtlp(string container,string mime,string uri)
    {
        const string image="https://private.example/patient.png";
        string body="{\"contents\":[{\"parts\":[{\""+container+"\":{\""+mime+"\":\"image/png\",\""+uri+"\":\""+image+"\"}}]}]}";
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        var options=Options(mask:new TraceConfig { HideInputImages=true }); options.BaseUrl=server.Url;
        using var agent=new Agent(new AutoConfig(options)); using var client=new HttpClient();
        using var response=await client.PostAsync(server.Url+"v1beta/models/m:generateContent",new StringContent(body,Encoding.UTF8,"application/json"));
        Assert.Equal("{}",await response.Content.ReadAsStringAsync()); Assert.True(agent.Flush(3000));
        Assert.DoesNotContain(image,Encoding.UTF8.GetString(Assert.Single(server.Exports)));
    }
    private sealed class GetterTrap
    {
        internal int Calls;
        public string Value { get { Calls++; throw new InvalidOperationException("must never run"); } }
        public override string ToString() { Calls++; throw new InvalidOperationException("must never run"); }
    }
    [Fact]
    public async Task AmbientMetadataNeverExecutesApplicationGettersBeforeTransport()
    {
        var trap=new GetterTrap(); var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using(ContextAttributes.UsingMetadata(new() { ["nested"]=trap,["safe"]="retained" }))
        using(var client=new HttpClient())
        using(var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json")))
            Assert.Equal("{}",await response.Content.ReadAsStringAsync());
        Assert.True(agent.Flush(3000)); Assert.Equal(0,trap.Calls);
        Assert.Contains(nameof(GetterTrap),Assert.IsType<string>(Assert.Single(exporter.Spans).GetTagItem("metadata")));
        Assert.Single(server.RequestHeaders);
    }
    [Theory]
    [InlineData("input.custom","inputs")] [InlineData("gen_ai.input.custom","inputs")]
    [InlineData("output.custom","outputs")] [InlineData("gen_ai.output.custom","outputs")]
    [InlineData("gen_ai.system_instructions.text","input-text")]
    [InlineData("gen_ai.tool.parameters","input-text")]
    [InlineData("gen_ai.tool.call","outputs")]
    [InlineData("gen_ai.tool.call.result.details","output-text")]
    [InlineData("gen_ai.request.parameters.temperature","parameters")]
    [InlineData("input.file.uri","images")]
    public void HideFlagsCoverEntireFamiliesOnAttributesEventsAndLinks(string key,string flag)
    {
        var exporter=new MemoryExporter(); var mask=new TraceConfig { HideInputs=flag=="inputs",HideOutputs=flag=="outputs",
            HideInputText=flag=="input-text",HideOutputText=flag=="output-text",HideInputImages=flag=="images",HideLlmInvocationParameters=flag=="parameters" };
        using var agent=new Agent(new AutoConfig(Options(exporter,mask)));
        using(var span=TraceAIAuto.Source.StartActivity("families",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),new ActivityTagsCollection { [key]=new[] { "PRIVATE_CONTENT" } }) }))
        {
            span!.SetTag(key,new[] { "PRIVATE_CONTENT" });
            span.AddEvent(new ActivityEvent("event",tags:new ActivityTagsCollection { [key]=new[] { "PRIVATE_CONTENT" } }));
        }
        Assert.True(agent.Flush(3000)); var safe=Assert.Single(exporter.Spans);
        Assert.Equal(TraceConfig.RedactedValue,safe.GetTagItem(key));
        Assert.Equal(TraceConfig.RedactedValue,Assert.Single(safe.Events).Tags.Single().Value);
        Assert.Equal(TraceConfig.RedactedValue,Assert.Single(safe.Links).Tags!.Single().Value);
    }
    [Fact]
    public void ImageHidingRecursesIntoSerializedJsonInsideContentStrings()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter,new TraceConfig { HideInputImages=true })));
        string inner="{\"image_url\":\"https://private.example/patient.png\"}";
        string value=System.Text.Json.JsonSerializer.Serialize(new { message=inner,content=new[] { inner } });
        using(var span=TraceAIAuto.Source.StartActivity("nested images")) span!.SetTag("input.value",value);
        Assert.True(agent.Flush(3000)); string actual=Assert.IsType<string>(Assert.Single(exporter.Spans).GetTagItem("input.value"));
        Assert.DoesNotContain("patient.png",actual); Assert.Contains(TraceConfig.RedactedValue,actual);
    }
    [Fact]
    public void LargeSharedStringsAreBoundedBeforeAnySanitizedCopiesAreAllocated()
    {
        var text=new string('x',2*1024*1024);
        var graph=Enumerable.Repeat<object>(text,16).ToArray();
        Assert.Equal(WirePrivacy.Redacted,WirePrivacy.Scrub(graph));
        Assert.Same(text,graph[0]);
    }
    [Fact]
    public void SanitizerBudgetFailurePreservesIdentityAndTimingButDropsAllContent()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        Activity original;
        using(original=TraceAIAuto.Source.StartActivity("too many")!)
        {
            original.SetTag("safe","retained-unless-failure"); original.TraceStateString="vendor=explicit-api-key";
            for(int i=0;i<5000;i++) original.AddEvent(new ActivityEvent("event explicit-secret-key"));
            original.SetStatus(ActivityStatusCode.Error,"explicit-api-key");
        }
        Assert.True(agent.Flush(3000)); var safe=Assert.Single(exporter.Spans);
        Assert.Equal(original.SpanId,safe.SpanId); Assert.Equal(original.TraceId,safe.TraceId); Assert.Equal(original.StartTimeUtc,safe.StartTimeUtc); Assert.Equal(original.Duration,safe.Duration);
        Assert.Empty(safe.TagObjects); Assert.Empty(safe.Events); Assert.Empty(safe.Links); Assert.Null(safe.TraceStateString); Assert.Null(safe.StatusDescription);
        Assert.Equal("retained-unless-failure",original.GetTagItem("safe")); Assert.Equal(5000,original.Events.Count());
    }
}
