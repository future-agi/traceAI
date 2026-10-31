using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

internal sealed class MemoryExporter : BaseExporter<Activity>
{
    internal readonly ConcurrentQueue<Activity> Spans=[];
    public override ExportResult Export(in Batch<Activity> batch) { foreach(var span in batch) Spans.Enqueue(span); return ExportResult.Success; }
}
public class RuntimeTests : InitIsolation
{
    private static AutoConfig Config(MemoryExporter exporter,Action<AutoOptions>? change=null)
    {
        var options=new AutoOptions { ApiKey="runtime-key", SecretKey="runtime-secret", Exporter=exporter, ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") };
        change?.Invoke(options); return new(options);
    }
    [Fact]
    public async Task AdditiveProviderKeepsApplicationExporterAndParentsFrameworkWireSpan()
    {
        var hostExporter=new MemoryExporter(); var fiExporter=new MemoryExporter();
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("Host.App").AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        using var agent=new Agent(Config(fiExporter));
        using var source=new ActivitySource("Host.App");
        await using var server=new MockServer(Encoding.UTF8.GetBytes("{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"response\"}}]}"),"application/json");
        ActivityTraceId trace; ActivitySpanId parentId;
        using(var parent=source.StartActivity("agent-request"))
        using(ContextAttributes.UsingSession("session-42"))
        using(ContextAttributes.UsingUser("user-42"))
        {
            Assert.NotNull(parent); parent.SetTag("gen_ai.span.kind","AGENT"); trace=parent.TraceId; parentId=parent.SpanId;
            using var client=new HttpClient(); using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"model\":\"m\",\"messages\":[]}",Encoding.UTF8,"application/json"));
            await response.Content.ReadAsStringAsync();
        }
        Assert.True(agent.Flush(3000));
        Assert.Single(hostExporter.Spans);
        var wire=Assert.Single(fiExporter.Spans); Assert.Equal(parentId,wire.ParentSpanId); Assert.Equal(trace,wire.TraceId);
        Assert.Equal(TraceConfig.RedactedValue,wire.GetTagItem("session.id")); Assert.Equal("user-42",wire.GetTagItem("user.id")); Assert.Equal(ActivityKind.Client,wire.Kind);
    }
    [Theory]
    [InlineData("otel")]
    [InlineData("legacy")]
    [InlineData("llm")]
    [InlineData("nested")]
    public async Task SuppressedOrNestedRequestsAreNotCaptured(string suppression)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using var activity=new Activity("manual-parent").Start();
        if(suppression=="llm") activity.SetTag("gen_ai.span.kind","LLM");
        if(suppression=="nested") activity.SetCustomProperty("traceai.wire.capture",true);
        using var scope=suppression=="otel"?SuppressInstrumentationScope.Begin():suppression=="legacy"?new SuppressTracing():null;
        using var client=new HttpClient(); using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
        await response.Content.ReadAsStringAsync(); Assert.True(agent.Flush(3000)); Assert.Empty(exporter.Spans);
    }
    [Fact]
    public void BuiltInActivityMappingAndMaskingApplyBeforeExport()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter,o=>o.TraceConfig=new TraceConfig { HideInputMessages=true,HideOutputs=true }));
        using var source=new ActivitySource("OpenAI.Chat");
        using(var activity=source.StartActivity("chat gpt",ActivityKind.Client,default(ActivityContext),new ActivityTagsCollection { ["gen_ai.operation.name"]="chat",["gen_ai.system"]="openai" }))
        {
            Assert.NotNull(activity); Assert.Equal("LLM",activity.GetTagItem("gen_ai.span.kind"));
            activity.SetTag("gen_ai.input.messages.0.message.content","private"); activity.SetTag("output.value","private");
            activity.SetTag("gen_ai.request.parameters","{\"api_key\":\"foreign-api-key\",\"seed\":2}");
            activity.SetTag("input.value","runtime-key");
        }
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal("__REDACTED__",span.GetTagItem("gen_ai.input.messages.0.message.content")); Assert.Equal("__REDACTED__",span.GetTagItem("output.value"));
        Assert.Equal(TraceConfig.RedactedValue,span.GetTagItem("input.value")); Assert.DoesNotContain("foreign-api-key",span.GetTagItem("gen_ai.request.parameters")!.ToString());
        Assert.Equal("openai",span.GetTagItem("gen_ai.provider.name"));
    }
    [Fact]
    public void ImmutableSensitiveLibraryEventsAreSanitizedOnDetachedSnapshots()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter,o=>o.TraceConfig=new TraceConfig { HideInputs=true }));
        using var source=new ActivitySource("OpenAI.Chat");
        using(var activity=source.StartActivity("chat"))
            activity!.AddEvent(new ActivityEvent("request",tags:new ActivityTagsCollection { ["input.value"]="private" }));
        Assert.True(agent.Flush(3000));
        Assert.Equal(TraceConfig.RedactedValue,Assert.Single(Assert.Single(exporter.Spans).Events).Tags.Single().Value);
    }
    [Theory]
    [InlineData("openai.completions","https://api.openai.com/v1/completions","{\"model\":\"m\",\"prompt\":\"hi\"}","{\"choices\":[{\"text\":\"ok\"}]}","ok")]
    [InlineData("openai.images","https://api.openai.com/v1/images/generations","{\"model\":\"m\",\"prompt\":\"draw\"}","{\"data\":[{\"b64_json\":\"SECRET-BASE64\"}]}","base64 omitted")]
    [InlineData("openai.moderations","https://api.openai.com/v1/moderations","{\"input\":\"hi\"}","{\"results\":[{\"flagged\":true}]}","flagged")]
    [InlineData("cohere.embed","https://api.cohere.com/v2/embed","{\"model\":\"m\",\"texts\":[\"hi\"]}","{\"embeddings\":{\"float\":[[1,2,3]]}}",null)]
    [InlineData("gemini.embed_content","https://generativelanguage.googleapis.com/v1beta/models/m:embedContent","{\"content\":{\"parts\":[{\"text\":\"hi\"}]}}","{\"embedding\":{\"values\":[1,2,3]}}",null)]
    [InlineData("ollama.embed","http://localhost:11434/api/embed","{\"model\":\"m\",\"input\":\"hi\"}","{\"embeddings\":[[1,2,3]]}",null)]
    [InlineData("ollama.generate","http://localhost:11434/api/generate","{\"model\":\"m\",\"prompt\":\"hi\"}","{\"response\":\"ok\",\"done\":true}","ok")]
    [InlineData("bedrock.invoke","https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke","{\"inputText\":\"hi\"}","{\"results\":[{\"outputText\":\"ok\"}]}","outputText")]
    public void FormatsNotCoveredByGoldenResponsesAreImplemented(string format,string url,string request,string response,string? output)
    {
        var req=new RequestData { Url=url,Body=Encoding.UTF8.GetBytes(request) }; var m=WireCore.Match("POST",url);
        Assert.NotNull(m); Assert.Equal(format,WireCore.S(m.Endpoint["format"]));
        var result=WireCore.Build(m,req,new ResponseData { Status=200,Body=Encoding.UTF8.GetBytes(response) }); Assert.NotNull(result);
        if(output!=null) Assert.Contains(output,result.Attributes["output.value"].ToString()); else Assert.Equal(3,result.Attributes["gen_ai.embeddings.dimension.count"]);
        Assert.DoesNotContain("SECRET-BASE64",string.Join("|",result.Attributes.Values));
    }
    [Fact]
    public void FlatTomlPrecedenceAndSecretMasking()
    {
        var path=Path.Combine(WireFixtureTests.Repo,"csharp","tests","TraceAI.AutoInstrumentation.Tests","config-"+Guid.NewGuid()+".toml");
        var old=Environment.GetEnvironmentVariable("FI_PROJECT_NAME");
        try
        {
            File.WriteAllText(path,"project_name = \"file#name\" # comment\nsample_rate = 0.25\ninstrumentations = [\"openai\", \"wire\"]\ndebug = true\n[tables]\nwire_capture = false\n");
            Environment.SetEnvironmentVariable("FI_PROJECT_NAME","environment");
            var config=new AutoConfig(new AutoOptions { ConfigFile=path,ApiKey="secret123",SecretKey="secret456",ProjectName="code" });
            Assert.Equal("code",config.Get("PROJECT_NAME")); Assert.Equal(0.25,config.SampleRate); Assert.True(config.Bool("AUTO_DEBUG")); Assert.False(config.Enabled("wire"));
            Assert.True(config.Enabled("openai")); Assert.False(config.Enabled("agents"));
            Assert.Equal("***",config.Masked()["FI_API_KEY"]);
            var envConfig=new AutoConfig(new AutoOptions { ConfigFile=path }); Assert.Equal("environment",envConfig.Get("PROJECT_NAME"));
            Environment.SetEnvironmentVariable("FI_PROJECT_NAME",null);
            var fileConfig=new AutoConfig(new AutoOptions { ConfigFile=path }); Assert.Equal("file#name",fileConfig.Get("PROJECT_NAME"));
        }
        finally { Environment.SetEnvironmentVariable("FI_PROJECT_NAME",old); File.Delete(path); }
    }
    [Fact]
    public void OneLineInitIsIdempotentAndDoctorMasksSecrets()
    {
        var exporter=new MemoryExporter();
        try
        {
            TraceAIAuto.Init(o=> { o.ApiKey="one-line-key"; o.SecretKey="one-line-secret"; o.Exporter=exporter; });
            TraceAIAuto.Init();
            using(var span=TraceAIAuto.Source.StartActivity("one-line")) Assert.NotNull(span);
            Assert.True(TraceAIAuto.Flush()); Assert.Single(exporter.Spans);
            var doctor=TraceAIAuto.Doctor(); Assert.DoesNotContain("one-line-key",doctor); Assert.DoesNotContain("one-line-secret",doctor);
            Assert.Contains("custom_exporter",doctor); Assert.Contains("observe",doctor);
            Assert.Contains(TraceAIAuto.GetStatus(),s=>s.Name=="wire"&&s.Status=="instrumented");
            Assert.Contains("wire",TraceAIAuto.Bootstrap());
        }
        finally { TraceAIAuto.Shutdown(); }
    }
    [Fact]
    public void SamplingIsRespectedEvenWhenHostRecordsTheSameSource()
    {
        var hostExporter=new MemoryExporter(); var agentExporter=new MemoryExporter();
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("OpenAI.Shared").AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        using var agent=new Agent(Config(agentExporter,o=>o.SampleRate=0));
        using var source=new ActivitySource("OpenAI.Shared");
        using(var activity=source.StartActivity("chat")) Assert.NotNull(activity);
        Assert.True(agent.Flush(3000)); Assert.Single(hostExporter.Spans); Assert.Empty(agentExporter.Spans);
    }
    [Fact]
    public async Task RequestTimeContextIsNotReplacedByReaderContext()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using var client=new HttpClient(); HttpResponseMessage response;
        using(ContextAttributes.UsingSession("request-session"))
        using(ContextAttributes.UsingUser("request-user"))
        {
            using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") { Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
            response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
        }
        using(response)
        using(ContextAttributes.UsingSession("reader-session"))
        using(ContextAttributes.UsingUser("reader-user")) await response.Content.ReadAsStringAsync();
        Assert.True(agent.Flush(3000)); Assert.Equal("request-user",Assert.Single(exporter.Spans).GetTagItem("user.id"));
        Assert.Equal(TraceConfig.RedactedValue,Assert.Single(exporter.Spans).GetTagItem("session.id"));
    }
    [Fact]
    public async Task ProviderKeysEchoedInErrorsAreRedactedWithoutChangingTheResponse()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        const string payload="{\"error\":{\"message\":\"bad provider-secret\"}}";
        await using var server=new MockServer(Encoding.UTF8.GetBytes(payload),"application/json") { StatusCode=401 };
        using var client=new HttpClient(); client.DefaultRequestHeaders.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer","provider-secret");
        using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
        Assert.Equal(payload,await response.Content.ReadAsStringAsync()); Assert.True(agent.Flush(3000));
        var span=Assert.Single(exporter.Spans); Assert.Equal(ActivityStatusCode.Error,span.Status);
        Assert.DoesNotContain("provider-secret",span.StatusDescription); Assert.Equal("401",span.GetTagItem("error.type"));
    }
    [Fact]
    public void CatalogMatchingAndUsagePreserveLargeIntegerValues()
    {
        var req=new RequestData { Url="https://api.openai.com/v1/chat/completions",Body="{\"messages\":[],\"seed\":9007199254740993}"u8.ToArray() };
        var result=WireCore.Build(WireCore.Match("POST",req.Url)!,req,new ResponseData { Status=200,Body="{\"usage\":{\"prompt_tokens\":9007199254740993,\"completion_tokens\":1}}"u8.ToArray() });
        Assert.NotNull(result); Assert.Equal(9007199254740993L,result.Attributes["gen_ai.request.seed"]);
        Assert.Equal(9007199254740994L,result.Attributes["gen_ai.usage.total_tokens"]);
    }
    [Fact]
    public void NestedAudioAndCredentialFieldsAreNeverExported()
    {
        var processor=new AutoProcessor(Config(new MemoryExporter()));
        var masked=processor.Mask("input.value","{\"apiKey\":\"foreign-key\",\"messages\":[{\"content\":[{\"type\":\"input_audio\",\"input_audio\":{\"data\":\"RAW-AUDIO\",\"format\":\"wav\"}}]}]}")!.ToString();
        Assert.DoesNotContain("foreign-key",masked); Assert.DoesNotContain("RAW-AUDIO",masked);
        Assert.Equal(TraceConfig.RedactedValue,processor.Mask("gen_ai.embeddings.vectors",new[]{1.0,2.0}));
    }
    private sealed class UntouchableContent : HttpContent
    {
        protected override bool TryComputeLength(out long length) => throw new InvalidOperationException("must not compute length during capture");
        protected override Task SerializeToStreamAsync(Stream stream,System.Net.TransportContext? context) => throw new InvalidOperationException("must not serialize during capture");
    }
    [Fact]
    public void CustomRequestContentIsNotReadSerializedOrAskedToComputeLength()
    {
        using var content=new UntouchableContent(); content.Headers.ContentType=new System.Net.Http.Headers.MediaTypeHeaderValue("application/json");
        var request=new RequestData(); HttpCapture.ReadRequest(content,request,1024);
        Assert.Null(request.Body); Assert.Null(request.BodySize);
    }
    [Fact]
    public async Task TransportFailureIsExportedWithoutSwallowingTheApplicationException()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using var client=new HttpClient();
        await Assert.ThrowsAsync<HttpRequestException>(()=>client.PostAsync("http://127.0.0.1:1/v1/chat/completions",new StringContent("{\"model\":\"m\",\"messages\":[]}",Encoding.UTF8,"application/json")));
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal(ActivityStatusCode.Error,span.Status); Assert.Equal("HttpRequestException",span.GetTagItem("error.type")); Assert.Equal("chat m",span.DisplayName);
    }
    [Fact]
    public void BodyLimitAndAudioCountingNeverStoreAudioBytes()
    {
        var req=new RequestData { Url="https://api.openai.com/v1/audio/speech",Body="{\"model\":\"m\",\"input\":\"hello\"}"u8.ToArray() };
        var capture=new Capture(WireCore.Match("POST",req.Url)!,req,4,default,DateTimeOffset.UtcNow,[]);
        capture.Response.Headers["content-type"]="audio/wav"; capture.Bytes(new byte[100]);
        Assert.Equal(100,capture.Response.Size); Assert.Null(capture.Response.Body); Assert.False(capture.Response.Truncated);
        capture.Complete(); Assert.Null(capture.Response.Body);
    }
}
