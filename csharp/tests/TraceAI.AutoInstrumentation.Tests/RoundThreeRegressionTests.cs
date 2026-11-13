using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using FIInstrumentation;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundThreeRegressionTests : InitIsolation
{
    private static AutoOptions Options(BaseExporter<Activity>? exporter=null,TraceConfig? mask=null) => new() {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,TraceConfig=mask,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","no-such-config.toml") };
    private sealed class Enrich(Action<Activity> action) : BaseProcessor<Activity>
    { public override void OnEnd(Activity activity)=>action(activity); }
    private sealed class BlockingGetter : IDisposable
    {
        internal int Calls;
        internal readonly TaskCompletionSource Entered=new(TaskCreationOptions.RunContinuationsAsynchronously);
        internal readonly ManualResetEventSlim Release=new();
        public string Content { get { Interlocked.Increment(ref Calls); Entered.TrySetResult(); Release.Wait(); return "PRIVATE_GETTER_RESULT"; } }
        public void Dispose() { Release.Set(); Release.Dispose(); }
    }
    public static IEnumerable<object[]> GetterCases =>
        from placement in new[] { "tag","event","link" }
        from kind in new[] { "object","json-value","json-object","json-array" }
        from late in new[] { false,true }
        select new object[] { placement,kind,late };
    [Theory]
    [MemberData(nameof(GetterCases))]
    public async Task PrivacyNeverExecutesApplicationGettersEvenInImmutableOrJsonValues(string placement,string kind,bool late)
    {
        using var value=new BlockingGetter();
        object payload=kind switch {
            "json-value"=>JsonValue.Create(value)!,
            "json-object"=>new JsonObject { ["nested"]=JsonValue.Create(value),["retained"]="safe" },
            "json-array"=>new JsonArray(JsonValue.Create(value)),
            _=>value
        };
        var exporter=new MemoryExporter(); var privacy=new AutoProcessor(new AutoConfig(Options(exporter)));
        void Attach(Activity activity)
        {
            if(placement=="tag") activity.SetTag("custom",payload);
            else if(placement=="event") activity.AddEvent(new ActivityEvent("event",tags:new ActivityTagsCollection { ["custom"]=payload }));
            else activity.AddLink(new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),
                new ActivityTagsCollection { ["custom"]=payload }));
        }
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .AddProcessor(privacy).AddProcessor(new Enrich(a=> { if(late) Attach(a); }))
            .AddProcessor(new SimpleActivityExportProcessor(new BorrowedExporter(exporter,privacy,new ExportCompletion()))).Build();
        var work=Task.Run(()=> {
            using var activity=TraceAIAuto.Source.StartActivity("getter safety"); Assert.NotNull(activity);
            if(!late) Attach(activity);
        });
        try
        {
            // Whichever finishes first is a deterministic signal, not a timing guess.
            Assert.Same(work,await Task.WhenAny(work,value.Entered.Task).WaitAsync(TimeSpan.FromSeconds(10)));
            await work;
        }
        finally { value.Release.Set(); await work.WaitAsync(TimeSpan.FromSeconds(10)); }
        Assert.Equal(0,value.Calls); Assert.False(value.Entered.Task.IsCompleted);
        var span=Assert.Single(exporter.Spans);
        var safe=placement=="tag"?span.GetTagItem("custom"):placement=="event"?
            Assert.Single(span.Events).Tags.Single().Value:Assert.Single(span.Links).Tags!.Single().Value;
        Assert.Contains(nameof(BlockingGetter),Assert.IsType<string>(safe));
        Assert.DoesNotContain("PRIVATE_GETTER_RESULT",(string)safe);
    }
    [Fact]
    public void OrdinaryImmutablePrimitiveAndArrayValuesDoNotGetDropped()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        using(var activity=TraceAIAuto.Source.StartActivity("safe immutable",ActivityKind.Internal,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),
                new ActivityTagsCollection { ["strings"]=new[] { "one","two" },["number"]=42 }) }))
        {
            Assert.NotNull(activity);
            activity.AddEvent(new ActivityEvent("event",tags:new ActivityTagsCollection { ["numbers"]=new[] { 1,2 },["bool"]=true }));
        }
        Assert.True(agent.Flush(3000)); Assert.Single(exporter.Spans);
    }
    [Theory]
    [InlineData("x-api-key",false)] [InlineData("api-key",false)] [InlineData("xi-api-key",false)]
    [InlineData("x-goog-api-key",false)] [InlineData("anthropic-api-key",false)] [InlineData("Ocp-Apim-Subscription-Key",false)]
    [InlineData("x-api-key",true)]
    public async Task CredentialEchoRedactionTrimsOptionalHttpWhitespaceForEachValue(string header,bool contentHeader)
    {
        const string first="FIRST_PRIVATE_KEY",second="SECOND_PRIVATE_KEY";
        await using var server=new MockServer(Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"bad "+first+" and "+second+"\"}}"),"application/json") { StatusCode=401 };
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") {
            Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        var headers=contentHeader?request.Content.Headers:(System.Net.Http.Headers.HttpHeaders)request.Headers;
        Assert.True(headers.TryAddWithoutValidation(header,new[] { "  "+first+" \t","\t "+second+"  " }));
        using var response=await client.SendAsync(request); await response.Content.ReadAsStringAsync();
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal("bad <redacted> and <redacted>",span.StatusDescription);
        string received=Assert.Single(server.RequestHeaders)[header];
        Assert.Contains(first,received); Assert.Contains(second,received);
        Assert.DoesNotContain(first,WireCore.J(span.TagObjects)); Assert.DoesNotContain(second,WireCore.J(span.TagObjects));
    }
    [Fact]
    public void AuthorizationWhitespaceIsTrimmedBeforeRemovingItsScheme()
    {
        Assert.Equal(new[] { "Bearer \tFIRST_PRIVATE_KEY","FIRST_PRIVATE_KEY" },WirePrivacy.HeaderSecrets(new Dictionary<string,string> {
            ["Authorization"]=" \tBearer \tFIRST_PRIVATE_KEY \t" }));
    }
    [Theory]
    [InlineData("inputs")] [InlineData("input-text")] [InlineData("input-messages")]
    [InlineData("outputs")] [InlineData("output-text")] [InlineData("output-messages")]
    [InlineData("none")]
    public async Task NativeGenAiContentObeysSideSpecificHideFlagsInActualOtlp(string setting)
    {
        await using var collector=new MockServer([],"application/json");
        var options=Options(mask:new TraceConfig {
            HideInputs=setting=="inputs",HideInputText=setting=="input-text",HideInputMessages=setting=="input-messages",
            HideOutputs=setting=="outputs",HideOutputText=setting=="output-text",HideOutputMessages=setting=="output-messages" });
        options.BaseUrl=collector.Url; using var agent=new Agent(new AutoConfig(options));
        using var source=new ActivitySource("OpenAI.Native.Content");
        using(var activity=source.StartActivity("execute_tool native"))
        {
            Assert.NotNull(activity);
            activity.SetTag("gen_ai.system_instructions","PRIVATE_SYSTEM_PROMPT");
            activity.SetTag("gen_ai.tool.call.arguments","PRIVATE_TOOL_INPUT");
            activity.SetTag("gen_ai.tool.call.result","PRIVATE_TOOL_OUTPUT");
            activity.SetTag("input.value","PRIVATE_RAW_INPUT"); activity.SetTag("output.value","PRIVATE_RAW_OUTPUT");
        }
        Assert.True(agent.Flush(3000)); var span=Assert.Single(collector.Exports.SelectMany(Otlp.Spans));
        bool input=setting.StartsWith("input"),output=setting.StartsWith("output");
        Assert.Equal(input?TraceConfig.RedactedValue:"PRIVATE_SYSTEM_PROMPT",span.Attributes["gen_ai.system_instructions"]);
        Assert.Equal(input?TraceConfig.RedactedValue:"PRIVATE_TOOL_INPUT",span.Attributes["gen_ai.tool.call.arguments"]);
        Assert.Equal(output?TraceConfig.RedactedValue:"PRIVATE_TOOL_OUTPUT",span.Attributes["gen_ai.tool.call.result"]);
        Assert.Equal(input?TraceConfig.RedactedValue:"PRIVATE_RAW_INPUT",span.Attributes["input.value"]);
        Assert.Equal(output?TraceConfig.RedactedValue:"PRIVATE_RAW_OUTPUT",span.Attributes["output.value"]);
    }
    [Theory]
    [InlineData("anthropic",true)] [InlineData("anthropic",false)]
    [InlineData("openai",true)] [InlineData("openai",false)]
    public async Task ImageHidingRecognizesTypedAnthropicBlocksAsWellAsOpenAiProperties(string format,bool hide)
    {
        const string url="https://private.example/patient-image.jpg";
        string body=format=="anthropic"?
            "{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"retained text\"},{\"type\":\"image\",\"source\":{\"type\":\"url\",\"url\":\""+url+"\"}}]}]}":
            "{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"retained text\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\""+url+"\"}}]}]}";
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        var options=Options(mask:new TraceConfig { HideInputImages=hide }); options.BaseUrl=server.Url;
        using var agent=new Agent(new AutoConfig(options)); using var client=new HttpClient();
        using var response=await client.PostAsync(server.Url+(format=="anthropic"?"v1/messages":"v1/chat/completions"),new StringContent(body,Encoding.UTF8,"application/json"));
        await response.Content.ReadAsStringAsync(); Assert.True(agent.Flush(3000));
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans)); string actual=WireCore.J(span.Attributes);
        if(hide) { Assert.DoesNotContain(url,actual); Assert.Contains(TraceConfig.RedactedValue,actual); }
        else Assert.Contains(url,actual);
        Assert.Contains("retained text",actual);
    }
    [Theory]
    [InlineData(true,"embedding")] [InlineData(false,"embedding")]
    [InlineData(true,"embeddings")] [InlineData(false,"embeddings")]
    public async Task BedrockGenericJsonFallbackNeverExportsNestedEmbeddingVectors(bool hide,string key)
    {
        byte[] bytes=Encoding.UTF8.GetBytes("{\"nested\":{\""+key+"\":[0.112233,0.223344,0.334455]},\"inputTextTokenCount\":1}");
        await using var server=new MockServer(bytes,"application/json");
        var options=Options(mask:new TraceConfig { HideEmbeddingVectors=hide }); options.BaseUrl=server.Url;
        using var agent=new Agent(new AutoConfig(options));
        // Keep the catalog host for matching while routing the real socket locally.
        using var handler=new SocketsHttpHandler { UseProxy=false,ConnectCallback=async(_,token)=> {
            var socket=new Socket(SocketType.Stream,ProtocolType.Tcp);
            try { await socket.ConnectAsync(new Uri(server.Url).Host,new Uri(server.Url).Port,token); return new NetworkStream(socket,ownsSocket:true); }
            catch { socket.Dispose(); throw; }
        } };
        using var client=new HttpClient(handler);
        using var request=new HttpRequestMessage(HttpMethod.Post,"http://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-embed-text-v1/invoke") {
            Content=new StringContent("{\"inputText\":\"hello\"}",Encoding.UTF8,"application/json") };
        request.Headers.Host=new Uri(server.Url).Authority; // HttpListener's loopback prefix.
        using var response=await client.SendAsync(request);
        Assert.Equal(HttpStatusCode.OK,response.StatusCode); Assert.Equal(bytes,await response.Content.ReadAsByteArrayAsync());
        Assert.True(agent.Flush(3000)); var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans));
        string output=Assert.IsType<string>(span.Attributes["output.value"]);
        Assert.DoesNotContain("0.112233",output); Assert.DoesNotContain("0.223344",output); Assert.DoesNotContain("0.334455",output);
        Assert.Contains("<redacted>",output); Assert.Contains("inputTextTokenCount",output);
    }
    [Theory]
    [InlineData("FI_CLIENT_SECRET")] [InlineData("FI_CLIENT_SECRET_EXTRA")] [InlineData("FI_OTHER_CREDENTIAL")]
    [InlineData("FI_AUTHORIZATION")] [InlineData("FI_CLIENT_PASSWORD")] [InlineData("FI_CLIENT_TOKEN")]
    public void DoctorAndStartupDebugMaskImportedSecretNamedConfiguration(string key)
    {
        const string secret="UNRELATED_CLIENT_SECRET_9876";
        var old=Environment.GetEnvironmentVariable(key); var error=Console.Error; using var debug=new StringWriter();
        try
        {
            Environment.SetEnvironmentVariable(key,secret); Console.SetError(debug);
            TraceAIAuto.Init(o=> { o.ApiKey="explicit-api-key"; o.SecretKey="explicit-secret-key"; o.Exporter=new MemoryExporter(); o.Debug=true; });
            var doctor=TraceAIAuto.Doctor(); Assert.DoesNotContain(secret,doctor); Assert.DoesNotContain(secret,debug.ToString());
            Assert.Equal(WirePrivacy.Redacted,JsonNode.Parse(doctor)!["configuration"]![key]!.GetValue<string>());
            Assert.Equal(WirePrivacy.Redacted,JsonNode.Parse(debug.ToString())!["configuration"]![key]!.GetValue<string>());
        }
        finally { TraceAIAuto.Shutdown(); Console.SetError(error); Environment.SetEnvironmentVariable(key,old); }
    }
    private sealed class FailingDestination : MemoryStream
    {
        internal readonly IOException Failure=new("application destination failed");
        public override void Write(byte[] buffer,int offset,int count)=>throw Failure;
        public override void Write(ReadOnlySpan<byte> buffer)=>throw Failure;
        public override Task WriteAsync(byte[] buffer,int offset,int count,CancellationToken token)=>Task.FromException(Failure);
        public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer,CancellationToken token=default)=>ValueTask.FromException(Failure);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task RealHttpCopyDestinationFailuresAreOkIncompleteConsumerAborts(bool sync)
    {
        await using var server=new MockServer("data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n"u8.ToArray(),"text/event-stream");
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") {
            Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead); using var target=new FailingDestination();
        Exception? failure=sync?Record.Exception(()=>response.Content.CopyTo(target,null,CancellationToken.None)):
            await Record.ExceptionAsync(()=>response.Content.CopyToAsync(target));
        Assert.NotNull(failure); Assert.Same(target.Failure,failure is HttpRequestException?failure.InnerException:failure);
        Assert.True(agent.Flush(3000)); var span=Assert.Single(exporter.Spans);
        Assert.Equal(ActivityStatusCode.Ok,span.Status); Assert.Equal(true,span.GetTagItem("traceai.wire.incomplete")); Assert.Null(span.GetTagItem("error.type"));
    }
    [Theory]
    [InlineData("otel")] [InlineData("legacy")]
    public async Task DeferredWireCompletionPreservesRequestTimeSuppressionDecision(string scope)
    {
        await using var server=new MockServer("{\"choices\":[{\"message\":{\"content\":\"hello\"}}]}"u8.ToArray(),"application/json");
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter))); using var client=new HttpClient();
        var requestAmbient=new AsyncLocal<string?> { Value="request" };
        using var source=new ActivitySource("unsubscribed-parent");
        using var listener=new ActivityListener { ShouldListenTo=s=>s.Name==source.Name,Sample=(ref ActivityCreationOptions<ActivityContext> _)=>ActivitySamplingResult.AllDataAndRecorded };
        ActivitySource.AddActivityListener(listener); using var parent=source.StartActivity("parent"); Assert.NotNull(parent);
        using var first=await client.SendAsync(new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") {
            Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") },HttpCompletionOption.ResponseHeadersRead);
        requestAmbient.Value="reader";
        using(scope=="otel"?(IDisposable)SuppressInstrumentationScope.Begin():new SuppressTracing())
        {
            await first.Content.ReadAsStringAsync();
            Assert.True(InstrumentationSuppression.Active); Assert.Equal("reader",requestAmbient.Value); Assert.Same(parent,Activity.Current);
            // Conversely, suppressed starts must stay excluded when read later.
            using var suppressed=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            await suppressed.Content.ReadAsStringAsync();
        }
        using var second=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
        await second.Content.ReadAsStringAsync(); Assert.True(agent.Flush(3000)); Assert.Equal(2,exporter.Spans.Count);
        Assert.All(exporter.Spans,s=>Assert.Equal(parent.SpanId,s.ParentSpanId));
    }
    [Fact]
    public void DeferredCompletionAlsoPreservesDecisionWhenExecutionContextFlowWasDisabled()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(Options(exporter)));
        Capture capture;
        using(ExecutionContext.SuppressFlow())
        {
            var request=new RequestData { Url="https://api.openai.com/v1/chat/completions",Body="{\"messages\":[]}"u8.ToArray() };
            capture=new Capture(WireCore.Match("POST",request.Url)!,request,4096,default,DateTimeOffset.UtcNow,[]);
            Assert.True(ExecutionContext.IsFlowSuppressed());
        }
        capture.Response.Status=200;
        using(SuppressInstrumentationScope.Begin()) { capture.Complete(); Assert.True(InstrumentationSuppression.Active); }
        Assert.True(agent.Flush(3000)); Assert.Single(exporter.Spans);
    }
}
