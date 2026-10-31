using System.Diagnostics;
using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class AdversarialTests : InitIsolation
{
    private const string Url="https://api.openai.com/v1/chat/completions";
    private const string Chunk="data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"}}]}\n\n";
    private static AutoConfig Config(BaseExporter<Activity> exporter,TraceConfig? mask=null) => new(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,TraceConfig=mask });
    private static RequestData Request(string? body="{\"messages\":[]}",string url=Url) => new() { Url=url,Body=body==null?null:Encoding.UTF8.GetBytes(body) };
    private static SpanResult? Build(RequestData req,ResponseData? resp=null) => WireCore.Build(WireCore.Match(req.Method,req.Url)!,req,resp??new ResponseData { Status=200 });
    internal static byte[] Compress(byte[] bytes,string encoding)
    {
        using var output=new MemoryStream();
        using(Stream compressor=encoding switch { "gzip"=>new GZipStream(output,CompressionLevel.Fastest,true),"deflate"=>new ZLibStream(output,CompressionLevel.Fastest,true),"raw-deflate"=>new DeflateStream(output,CompressionLevel.Fastest,true),_=>new BrotliStream(output,CompressionLevel.Fastest,true) }) compressor.Write(bytes);
        return output.ToArray();
    }
    [Theory]
    [InlineData("{\"input\":\"hello\"}")]
    [InlineData("{\"model\":\"stored\",\"prompt\":{\"id\":\"p\"}}")]
    public void ResponsesRequiresEitherInputOrModel(string body) => Assert.NotNull(Build(Request(body,"https://api.openai.com/v1/responses")));

    [Theory]
    [InlineData(true)] [InlineData(false)]
    public void OversizedBodyOnlyCapturesCatalogProviders(bool known)
    {
        var req=Request(null,known?Url:"https://proxy.invalid/v1/chat/completions"); req.Truncated=true;
        var result=Build(req); if(!known) Assert.Null(result); else { Assert.NotNull(result); Assert.Equal(true,result.Attributes["traceai.wire.truncated"]); Assert.False(result.Attributes.ContainsKey("input.value")); }
    }
    [Fact]
    public void MultipartCannotBypassChatBodyValidation()
    {
        var req=Request(null); req.Fields=new() { ["model"]="m",["unrelated"]="form" }; Assert.Null(Build(req));
    }
    [Fact]
    public void MultipartTextBudgetIsIndependentOfFileAndBodyLimit()
    {
        using var multipart=new MultipartFormDataContent(); multipart.Add(new StringContent("whisper-1"),"model"); multipart.Add(new StringContent(new string('p',70000)),"prompt");
        multipart.Add(new ByteArrayContent(new byte[8192]),"file","x.wav");
        var req=Request(null,"https://api.openai.com/v1/audio/transcriptions"); HttpCapture.ReadRequest(multipart,req,4);
        var result=Build(req); Assert.NotNull(result); Assert.Equal("whisper-1",result.Attributes["gen_ai.request.model"]);
        Assert.Equal(65536,Encoding.UTF8.GetByteCount(req.Fields!["prompt"])); Assert.Equal(8192L,result.Attributes["traceai.audio.input_bytes"]);
    }
    private sealed class OneShotStream(byte[] bytes) : MemoryStream(bytes)
    {
        internal int Reads;
        public override bool CanSeek=>false;
        public override int Read(byte[] buffer,int offset,int count) { Reads++; return base.Read(buffer,offset,count); }
        public override int Read(Span<byte> buffer) { Reads++; return base.Read(buffer); }
        public override ValueTask<int> ReadAsync(Memory<byte> buffer,CancellationToken cancellationToken=default) { Reads++; return base.ReadAsync(buffer,cancellationToken); }
    }
    [Fact]
    public async Task MultipartStreamingFieldsAndUnknownFileSizesAreObservedOnlyDuringSend()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        await using var server=new MockServer("{\"text\":\"transcribed\"}"u8.ToArray(),"application/json");
        using var multipart=new MultipartFormDataContent("test-boundary");
        var model=new OneShotStream("whisper-1"u8.ToArray()); var audio=new OneShotStream(new byte[100000]);
        multipart.Add(new StreamContent(model),"model"); multipart.Add(new StreamContent(audio),"file","x.wav");
        using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/audio/transcriptions") { Content=multipart };
        Assert.Equal(0,model.Reads); Assert.Equal(0,audio.Reads);
        using var client=new HttpClient(); using var response=await client.SendAsync(request);
        Assert.Contains("transcribed",await response.Content.ReadAsStringAsync()); Assert.True(agent.Flush(1000));
        var span=Assert.Single(exporter.Spans); Assert.Equal("whisper-1",span.GetTagItem("gen_ai.request.model")); Assert.Equal(100000L,span.GetTagItem("traceai.audio.input_bytes")); Assert.Equal("transcribed",span.GetTagItem("gen_ai.audio.transcript"));
    }
    [Fact]
    public void SynchronousResponseCopyPreservesApplicationBytes()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter)); var req=Request();
        var capture=new Capture(WireCore.Match("POST",Url)!,req,4194304,default,DateTimeOffset.UtcNow,[]); capture.Response.Status=200; capture.Response.Headers["content-type"]="text/event-stream";
        using var content=new ReadTapStream(new MemoryStream(Encoding.UTF8.GetBytes(Chunk)),capture); using var output=new MemoryStream();
        content.CopyTo(output); Assert.Equal(Chunk,Encoding.UTF8.GetString(output.ToArray())); Assert.True(agent.Flush(1000)); Assert.Equal("partial",Assert.Single(exporter.Spans).GetTagItem("output.value"));
    }
    [Fact]
    public void MultipartMetadataIsBounded()
    {
        using var multipart=new MultipartFormDataContent(); multipart.Add(new StringContent("whisper-1"),"model");
        for(int i=0;i<300;i++) multipart.Add(new ByteArrayContent([]),new string('n',2048)+i,new string('f',2048));
        var req=Request(null); HttpCapture.ReadRequest(multipart,req,4194304);
        Assert.True(req.Files.Count<=128); Assert.All(req.Files,f=> { Assert.True(f.Name.Length<=1024); Assert.True(f.Filename!.Length<=1024); });
    }
    [Theory]
    [InlineData("gzip")] [InlineData("deflate")] [InlineData("br")] [InlineData("raw-deflate")]
    public void CompressedStreamsPreserveOutput(string encoding)
    {
        var resp=new ResponseData { Status=200,Body=Compress(Encoding.UTF8.GetBytes(Chunk),encoding),Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-type"]="text/event-stream",["content-encoding"]=encoding=="raw-deflate"?"deflate":encoding } };
        Assert.Equal("partial",Build(Request(),resp)!.Attributes["output.value"]);
    }
    [Fact]
    public void UndecodableEncodingIsReportedWithoutRecordingEncodedBytes()
    {
        var resp=new ResponseData { Status=200,Body="encoded bytes"u8.ToArray(),Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-encoding"]="zstd" } };
        var result=Build(Request(),resp)!; Assert.Equal("zstd",result.Attributes["traceai.wire.undecoded_encoding"]); Assert.False(result.Attributes.ContainsKey("output.value"));
    }
    [Fact]
    public void TransportFailureRetainsPartialOutputAndError()
    {
        var resp=new ResponseData { Status=200,Body=Encoding.UTF8.GetBytes(Chunk),Error=new TimeoutException("reset"),Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-type"]="text/event-stream" } };
        var result=Build(Request(),resp)!; Assert.False(result.Ok); Assert.Equal("TimeoutException",result.Attributes["error.type"]); Assert.Equal("partial",result.Attributes["output.value"]);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task ZeroLengthReadIsNotEofAndDisposeRetainsPartialOutput(bool asyncDispose)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter)); var req=Request();
        var capture=new Capture(WireCore.Match("POST",Url)!,req,4194304,default,DateTimeOffset.UtcNow,[]); capture.Response.Status=200; capture.Response.Headers["content-type"]="text/event-stream";
        var stream=new ReadTapStream(new MemoryStream(Encoding.UTF8.GetBytes(Chunk+Chunk)),capture);
        Assert.Equal(0,stream.Read([])); Assert.Equal(0,await stream.ReadAsync(Memory<byte>.Empty)); Assert.True(agent.Flush(1000)); Assert.Empty(exporter.Spans);
        var bytes=new byte[Encoding.UTF8.GetByteCount(Chunk)]; Assert.Equal(bytes.Length,await stream.ReadAsync(bytes));
        if(asyncDispose) await stream.DisposeAsync(); else stream.Dispose();
        Assert.True(agent.Flush(1000)); var span=Assert.Single(exporter.Spans); Assert.Equal(ActivityStatusCode.Ok,span.Status); Assert.Equal("partial",span.GetTagItem("output.value"));
        Assert.Equal(true,span.GetTagItem("traceai.wire.incomplete")); Assert.Null(span.GetTagItem("traceai.wire.truncated"));
    }
    [Fact]
    public void ExtendedContentKeysNeverEnterParameters()
    {
        var req=Request("{\"messages\":[],\"instructions\":\"private\",\"audio_url\":\"private\",\"url\":\"private\",\"file_url\":\"private\",\"image_url\":\"private\",\"seed\":2}");
        var result=Build(req)!; Assert.DoesNotContain("private",result.Attributes["gen_ai.request.parameters"].ToString());
    }
    [Theory]
    [InlineData("data")] [InlineData("bytes")] [InlineData("file_data")] [InlineData("audio_base64")] [InlineData("b64_json")]
    public void InlineMediaIsOmittedAtEveryDepth(string key)
    {
        string payload=new('A',512);
        var body=new JsonObject { ["messages"]=new JsonArray(),["nested"]=new JsonObject { [key]=payload,["images"]=new JsonArray(payload),["url"]="data:image/png;base64,QUJD" } };
        var result=Build(Request(WireCore.J(body)))!; var all=WireCore.J(result.Attributes);
        Assert.DoesNotContain(payload,all); Assert.DoesNotContain("QUJD",all); Assert.Contains("<omitted: 512 base64 chars>",all); Assert.Contains("<omitted: 4 base64 chars>",all);
    }
    [Theory]
    [InlineData("authorization_token")] [InlineData("xi-api-key")] [InlineData("client_secret")] [InlineData("private-key")] [InlineData("refresh_token")]
    public void NestedCredentialKeysAreRedactedBeforeFlattening(string key)
    {
        var body=new JsonObject { ["messages"]=new JsonArray(new JsonObject { ["role"]="tool",["content"]=WireCore.J(new JsonObject { [key]="BODY_SECRET_123" }) }),["extra"]=new JsonArray(new JsonObject { [key]="BODY_SECRET_123" }) };
        Assert.DoesNotContain("BODY_SECRET_123",WireCore.J(Build(Request(WireCore.J(body)))!.Attributes));
    }
    [Theory]
    [InlineData("Token", "Authorization")] [InlineData("", "xi-api-key")] [InlineData("", "api-key")] [InlineData("", "Ocp-Apim-Subscription-Key")]
    public void CredentialHeadersAreRemovedFromErrorsAndAllAttributes(string scheme,string header)
    {
        var req=Request("{\"messages\":[{\"role\":\"user\",\"content\":\"ECHO_SECRET_123\"}]}"); req.Headers[header]=(scheme.Length>0?scheme+" ":"")+"ECHO_SECRET_123";
        var result=Build(req,new ResponseData { Status=401,Body="{\"error\":{\"message\":\"bad ECHO_SECRET_123\"}}"u8.ToArray() })!;
        Assert.Equal("bad <redacted>",result.Description); Assert.DoesNotContain("ECHO_SECRET_123",WireCore.J(result.Attributes));
    }
    [Fact]
    public void BodyCredentialsEchoedInProviderErrorsAreAlsoRedacted()
    {
        var req=Request("{\"messages\":[],\"mcp_servers\":[{\"authorization_token\":\"BODY_ECHO_SECRET_123\"}]}");
        var result=Build(req,new ResponseData { Status=401,Body="{\"error\":{\"message\":\"invalid BODY_ECHO_SECRET_123\"}}"u8.ToArray() })!;
        Assert.Equal("invalid <redacted>",result.Description);
    }
    [Fact]
    public void SignedUrlsAndCredentialContextArraysAreScrubbed()
    {
        var processor=new AutoProcessor(Config(new MemoryExporter()));
        var values=new[] { "https://s.example/a?sig=URL_SECRET_123&X-Amz-Credential=CREDENTIAL_123&x=ok", "explicit-api-key", "{\"authorization_token\":\"NESTED_SECRET_123\"}" };
        var result=WireCore.J(processor.Mask("metadata",values));
        foreach(var secret in new[] { "URL_SECRET_123","CREDENTIAL_123","explicit-api-key","NESTED_SECRET_123" }) Assert.DoesNotContain(secret,result);
    }
    [Theory]
    [InlineData("input")] [InlineData("output")]
    public void TextHideFlagsAlsoMaskToolArgumentsAndDefinitions(string side)
    {
        var mask=side=="input"?new TraceConfig { HideInputText=true }:new TraceConfig { HideOutputText=true };
        var processor=new AutoProcessor(Config(new MemoryExporter(),mask));
        Assert.Equal(TraceConfig.RedactedValue,processor.Mask($"gen_ai.{side}.messages.0.message.tool_calls.0.tool_call.function.arguments","private"));
        if(side=="input") { Assert.Equal(TraceConfig.RedactedValue,processor.Mask("gen_ai.tool.definitions.0.tool.json_schema","private")); Assert.Equal(TraceConfig.RedactedValue,processor.Mask("gen_ai.request.parameters","private")); }
    }
    [Fact]
    public void InvocationParameterMaskCannotLeakThroughRawRequestJson()
    {
        var processor=new AutoProcessor(Config(new MemoryExporter(),new TraceConfig { HideLlmInvocationParameters=true }));
        Assert.Equal(TraceConfig.RedactedValue,processor.Mask("input.value","{\"messages\":[],\"temperature\":0.7,\"max_tokens\":321}"));
    }
    [Theory]
    [InlineData("text_to_speech",true,false,true)] [InlineData("text_to_speech",false,true,false)]
    [InlineData("transcription",true,false,false)] [InlineData("transcription",false,true,true)]
    public void AudioTranscriptMaskUsesItsOwnSide(string operation,bool hideInput,bool hideOutput,bool hidden)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter,new TraceConfig { HideInputText=hideInput,HideOutputText=hideOutput }));
        using(var a=TraceAIAuto.Source.StartActivity("audio")) { a!.SetTag("gen_ai.operation.name",operation); a.SetTag("gen_ai.audio.transcript","private"); }
        Assert.True(agent.Flush(1000)); Assert.Equal(hidden?TraceConfig.RedactedValue:"private",Assert.Single(exporter.Spans).GetTagItem("gen_ai.audio.transcript"));
    }
    [Fact]
    public async Task RepeatedInitCannotDisableWireOrDuplicateHostExport()
    {
        var exporter=new MemoryExporter(); var hostExporter=new MemoryExporter();
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("OpenAI.Shared.Reconfigure").AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        try
        {
            void Configure(AutoOptions o) { o.ApiKey="code-api-key"; o.SecretKey="code-secret-key"; o.Exporter=exporter; }
            TraceAIAuto.Init(Configure); TraceAIAuto.Init(o=> { Configure(o); o.WireCapture=false; o.DisabledInstrumentations=["openai"]; });
            await using var server=new MockServer("{}"u8.ToArray(),"application/json"); using var client=new HttpClient();
            using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            using var source=new ActivitySource("OpenAI.Shared.Reconfigure"); using(var a=source.StartActivity("chat")) Assert.NotNull(a);
            Assert.True(TraceAIAuto.Flush(1000)); Assert.Equal(2,exporter.Spans.Count); Assert.Single(hostExporter.Spans);
            TraceAIAuto.Shutdown(); using(var a=source.StartActivity("chat")) Assert.NotNull(a); Assert.Equal(2,hostExporter.Spans.Count);
        }
        finally { TraceAIAuto.Shutdown(); }
    }
    [Fact]
    public void ExactProviderHostBeatsAnotherProvidersPortRule()
    {
        Assert.Equal("deepgram",WireCore.Match("POST","https://api.deepgram.com:11434/v1/listen")?.Provider);
        Assert.Equal("assemblyai",WireCore.Match("POST","https://api.assemblyai.com:11434/v2/transcript")?.Provider);
    }
    [Theory]
    [InlineData("{}")] [InlineData("not json")]
    public void PollingOnlyCapturesCompletedOrErrorBodies(string body)
    {
        var request=Request(null,"https://api.assemblyai.com/v2/transcript/poll123"); request.Method="GET";
        Assert.Null(Build(request,new ResponseData { Status=200,Body=Encoding.UTF8.GetBytes(body) }));
    }
    [Fact]
    public void BinaryBedrockStreamsOnlyRecordRequestSideAndStatus()
    {
        var request=Request("{\"messages\":[]}","https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse-stream");
        var response=new ResponseData { Status=200,Size=100,Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-type"]="application/vnd.amazon.eventstream" } };
        var result=Build(request,response)!; Assert.DoesNotContain(result.Attributes.Keys,k=>k.StartsWith("gen_ai.audio",StringComparison.Ordinal)); Assert.False(result.Attributes.ContainsKey("output.value"));
    }
    [Fact]
    public void NestedSerializedJsonCannotBypassThePrivacyDepthBudget()
    {
        JsonNode nested=new JsonObject { ["authorization_token"]="DEPTH_SECRET_123" };
        for(int i=0;i<40;i++) nested=new JsonObject { ["nested"]=nested };
        JsonNode outer=new JsonObject { ["arguments"]=WireCore.J(nested) };
        for(int i=0;i<40;i++) outer=new JsonObject { ["nested"]=outer };
        Assert.DoesNotContain("DEPTH_SECRET_123",WirePrivacy.ScrubString(WireCore.J(outer)));
    }
    [Fact]
    public void NestedSerializedJsonScrubbingIsValidAndIdempotent()
    {
        var embedded=WireCore.J(new JsonObject { ["url"]="https://s.example/a?sig=PRIVATE_SIG_123",["authorization_token"]="PRIVATE_TOKEN_123" });
        var value=WireCore.J(new JsonObject { ["messages"]=new JsonArray(new JsonObject { ["role"]="user",["content"]=embedded }) });
        var once=WirePrivacy.ScrubString(value); var twice=WirePrivacy.ScrubString(once);
        Assert.NotNull(WireCore.Parse(Encoding.UTF8.GetBytes(once))); Assert.Equal(once,twice);
        Assert.DoesNotContain("PRIVATE_SIG_123",once); Assert.DoesNotContain("PRIVATE_TOKEN_123",once);
    }
    [Theory]
    [InlineData(true)] [InlineData(false)]
    public void MessageHideFlagsCannotLeakThroughRawValues(bool input)
    {
        var processor=new AutoProcessor(Config(new MemoryExporter(),input?new TraceConfig { HideInputMessages=true }:new TraceConfig { HideOutputMessages=true }));
        Assert.Equal(TraceConfig.RedactedValue,processor.Mask(input?"input.value":"output.value","private"));
    }
    [Theory]
    [InlineData(true)] [InlineData(false)]
    public void UnavailableRequestConnectionFailuresCaptureOnlyCatalogProviders(bool known)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(new AutoConfig(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,WireCapture=false }));
        using var capture=new HttpCapture(Config(exporter));
        using var request=new HttpRequestMessage(HttpMethod.Post,known?Url:"https://unknown.invalid/v1/chat/completions") { Content=new StreamContent(new MemoryStream("do not read ahead"u8.ToArray())) };
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.Exception",new { Request=request,Exception=new HttpRequestException("connection failed") }));
        Assert.True(agent.Flush(1000));
        if(!known) Assert.Empty(exporter.Spans);
        else { var span=Assert.Single(exporter.Spans); Assert.Equal(ActivityStatusCode.Error,span.Status); Assert.Equal(true,span.GetTagItem("traceai.wire.body_unavailable")); Assert.Equal("HttpRequestException",span.GetTagItem("error.type")); }
    }
    [Fact]
    public async Task EncodedHttpResponseIsUnchangedButTelemetryIsDecoded()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter)); var bytes=Compress(Encoding.UTF8.GetBytes(Chunk),"gzip");
        await using var server=new MockServer(bytes,"text/event-stream"); server.ResponseHeaders["Content-Encoding"]="gzip";
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") { Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
        Assert.Equal(bytes,await response.Content.ReadAsByteArrayAsync()); Assert.True(agent.Flush(1000)); Assert.Equal("partial",Assert.Single(exporter.Spans).GetTagItem("output.value"));
    }
    private sealed class FailingStream(byte[] first) : MemoryStream(first)
    {
        public override int Read(Span<byte> buffer) => Position>=Length?throw new IOException("reset after partial output"):base.Read(buffer);
        public override ValueTask<int> ReadAsync(Memory<byte> buffer,CancellationToken cancellationToken=default) => Position>=Length?ValueTask.FromException<int>(new IOException("reset after partial output")):base.ReadAsync(buffer,cancellationToken);
    }
    [Theory]
    [InlineData(true)] [InlineData(false)]
    public async Task ReadFailuresPreservePartialTelemetryAndApplicationException(bool asynchronous)
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter)); var req=Request();
        var capture=new Capture(WireCore.Match("POST",Url)!,req,4194304,default,DateTimeOffset.UtcNow,[]); capture.Response.Status=200; capture.Response.Headers["content-type"]="text/event-stream";
        using var stream=new ReadTapStream(new FailingStream(Encoding.UTF8.GetBytes(Chunk)),capture); var bytes=new byte[4096];
        if(asynchronous) { Assert.True(await stream.ReadAsync(bytes)>0); await Assert.ThrowsAsync<IOException>(async()=>Assert.Equal(0,await stream.ReadAsync(bytes))); }
        else { Assert.True(stream.Read(bytes)>0); Assert.Throws<IOException>(()=>stream.Read(bytes)); }
        Assert.True(agent.Flush(1000)); var span=Assert.Single(exporter.Spans); Assert.Equal("partial",span.GetTagItem("output.value")); Assert.Equal(ActivityStatusCode.Error,span.Status); Assert.Equal("IOException",span.GetTagItem("error.type"));
    }
    [Fact]
    public void MultipartDelimiterPrefixesInFilesAreCountedExactlyAcrossSingleByteWrites()
    {
        var req=Request(null,"https://api.openai.com/v1/audio/transcriptions");
        using var multipart=new MultipartFormDataContent("boundary-test"); multipart.Add(new StringContent("whisper-1"),"model");
        var payload=Encoding.UTF8.GetBytes("abc\r\n--boundary-testINVALID\r\nmore bytes"); multipart.Add(new ByteArrayContent(payload),"file","clip.wav");
        using var wire=new MemoryStream(); multipart.CopyTo(wire,null,CancellationToken.None);
        var observer=MultipartObserver.Create(multipart,req)!;
        foreach(var b in wire.ToArray()) observer.Bytes(new[]{b});
        Assert.Equal("whisper-1",req.Fields!["model"]); Assert.Equal(payload.Length,Assert.Single(req.Files).Size); Assert.False(req.BodyUnavailable);
    }
    [Fact]
    public void CompressedBodyExpansionHonoursTheBufferLimit()
    {
        var response=new ResponseData { Status=200,Body=Compress(Encoding.UTF8.GetBytes(new string('x',100000)),"gzip"),MaxBody=1024,Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-encoding"]="gzip" } };
        var result=Build(Request(),response)!; Assert.Equal(true,result.Attributes["traceai.wire.truncated"]); Assert.False(result.Attributes.ContainsKey("output.value"));
    }
    [Fact]
    public void CompressedRequestBodiesAreValidatedAfterDecoding()
    {
        var request=Request(); request.Body=Compress(request.Body!,"gzip"); request.Headers["content-encoding"]="gzip";
        Assert.Contains("messages",Build(request)!.Attributes["input.value"].ToString());
    }
    [Fact]
    public async Task ExplicitConfigurationOverridesInvalidEnvironmentAndExporterAuth()
    {
        string[] keys=["FI_BASE_URL","FI_PROJECT_TYPE","FI_SAMPLE_RATE","FI_API_KEY","FI_SECRET_KEY","OTEL_EXPORTER_OTLP_HEADERS","OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"];
        var previous=keys.ToDictionary(k=>k,Environment.GetEnvironmentVariable);
        try
        {
            Environment.SetEnvironmentVariable("FI_BASE_URL","not a URL"); Environment.SetEnvironmentVariable("FI_PROJECT_TYPE","invalid"); Environment.SetEnvironmentVariable("FI_SAMPLE_RATE","invalid");
            Environment.SetEnvironmentVariable("FI_API_KEY","env-api-key"); Environment.SetEnvironmentVariable("FI_SECRET_KEY","env-secret-key");
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS","X-Api-Key=wrong-env-api,X-Secret-Key=wrong-env-secret");
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT","not a URL");
            await using var server=new MockServer([],"application/json");
            using var agent=new Agent(new AutoConfig(new AutoOptions { BaseUrl=server.Url,ProjectType="observe",SampleRate=1,ApiKey="code-api-key",SecretKey="code-secret-key",ProjectName="code-project" }));
            using(var span=TraceAIAuto.Source.StartActivity("explicit")) Assert.NotNull(span);
            Assert.True(agent.Flush(1000)); var headers=Assert.Single(server.ExportHeaders);
            Assert.Equal("code-api-key",headers["X-Api-Key"]); Assert.Equal("code-secret-key",headers["X-Secret-Key"]);
            var resource=Assert.Single(server.Exports.SelectMany(Otlp.Spans)).Resource;
            Assert.Equal("observe",resource["project_type"]); Assert.Equal("code-project",resource["project_name"]);
        }
        finally { foreach(var (key,value) in previous) Environment.SetEnvironmentVariable(key,value); }
    }
    [Fact]
    public async Task BorrowedOtlpExporterCanStillExportAfterAgentRetirement()
    {
        await using var server=new MockServer([],"application/json");
        using var exporter=new OpenTelemetry.Exporter.OtlpTraceExporter(new OpenTelemetry.Exporter.OtlpExporterOptions { Endpoint=new Uri(server.Url+"tracer/v1/traces"),Protocol=OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf });
        for(int i=0;i<2;i++)
        {
            using var agent=new Agent(Config(exporter)); using(var span=TraceAIAuto.Source.StartActivity("custom-otlp")) Assert.NotNull(span);
            Assert.True(agent.Flush(1000));
        }
        Assert.Equal(2,server.Exports.SelectMany(Otlp.Spans).Count());
    }
    [Fact]
    public void DefaultProjectNameUsesOtelServiceThenWorkingDirectory()
    {
        var project=Environment.GetEnvironmentVariable("FI_PROJECT_NAME"); var service=Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME");
        try
        {
            Environment.SetEnvironmentVariable("FI_PROJECT_NAME",null); Environment.SetEnvironmentVariable("OTEL_SERVICE_NAME","service-default");
            Assert.Equal("service-default",Config(new MemoryExporter()).Get("PROJECT_NAME")); Environment.SetEnvironmentVariable("OTEL_SERVICE_NAME",null);
            Assert.Equal(new DirectoryInfo(Environment.CurrentDirectory).Name,Config(new MemoryExporter()).Get("PROJECT_NAME"));
        }
        finally { Environment.SetEnvironmentVariable("FI_PROJECT_NAME",project); Environment.SetEnvironmentVariable("OTEL_SERVICE_NAME",service); }
    }
    [Fact]
    public void RepeatedInitKeepsInitialCredentialsAndCallerExporter()
    {
        var exporter=new MemoryExporter(); var api=Environment.GetEnvironmentVariable("FI_API_KEY"); var secret=Environment.GetEnvironmentVariable("FI_SECRET_KEY");
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY",null); Environment.SetEnvironmentVariable("FI_SECRET_KEY",null);
            TraceAIAuto.Init(o=> { o.ApiKey="explicit-api-key"; o.SecretKey="explicit-secret-key"; o.Exporter=exporter; });
            TraceAIAuto.Init(o=>o.WireCapture=false);
            using(var span=TraceAIAuto.Source.StartActivity("still-enabled")) Assert.NotNull(span);
            Assert.True(TraceAIAuto.Flush(1000)); Assert.Single(exporter.Spans); Assert.Contains(TraceAIAuto.GetStatus(),s=>s.Name=="wire"&&s.Status=="instrumented");
        }
        finally { TraceAIAuto.Shutdown(); Environment.SetEnvironmentVariable("FI_API_KEY",api); Environment.SetEnvironmentVariable("FI_SECRET_KEY",secret); }
    }
    [Fact]
    public async Task RepeatedInitIgnoresChangedEnvironment()
    {
        var exporter=new MemoryExporter(); var previous=Environment.GetEnvironmentVariable("FI_WIRE_CAPTURE");
        try
        {
            Environment.SetEnvironmentVariable("FI_WIRE_CAPTURE","true");
            TraceAIAuto.Init(o=> { o.ApiKey="explicit-api-key"; o.SecretKey="explicit-secret-key"; o.Exporter=exporter; });
            Environment.SetEnvironmentVariable("FI_WIRE_CAPTURE","false"); TraceAIAuto.Init();
            await using var server=new MockServer("{}"u8.ToArray(),"application/json"); using var client=new HttpClient();
            using var response=await client.PostAsync(server.Url+"v1/chat/completions",new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json"));
            Assert.True(TraceAIAuto.Flush(1000)); Assert.Single(exporter.Spans); Assert.Contains(TraceAIAuto.GetStatus(),s=>s.Name=="wire"&&s.Status=="instrumented");
        }
        finally { TraceAIAuto.Shutdown(); Environment.SetEnvironmentVariable("FI_WIRE_CAPTURE",previous); }
    }
    private sealed class CallbackExporter : BaseExporter<Activity>
    {
        internal Action? Callback;
        internal readonly ManualResetEventSlim Entered=new(), Release=new(true);
        internal int Exports, Shutdowns;
        public override ExportResult Export(in Batch<Activity> batch) { Interlocked.Increment(ref Exports); Entered.Set(); Release.Wait(TimeSpan.FromSeconds(10)); Callback?.Invoke(); return ExportResult.Success; }
        protected override bool OnShutdown(int timeoutMilliseconds) { Interlocked.Increment(ref Shutdowns); return true; }
    }
    [Fact]
    public async Task FlushAndShutdownDeadlinesSurviveABlockedExporter()
    {
        var exporter=new CallbackExporter(); exporter.Release.Reset(); var agent=new Agent(Config(exporter));
        try
        {
            // A full batch starts Export without a prior Flush task. Establish that
            // it is blocked before testing ForceFlush's timeout-boundary result.
            for(int i=0;i<512;i++) using(var span=TraceAIAuto.Source.StartActivity("blocked")) Assert.NotNull(span);
            Assert.True(exporter.Entered.Wait(5000));
            var clock=Stopwatch.StartNew(); Assert.False(agent.Flush(50)); Assert.True(clock.Elapsed<TimeSpan.FromMilliseconds(700));
            clock.Restart(); await Task.Run(()=>agent.Shutdown(50)).WaitAsync(TimeSpan.FromMilliseconds(700)); Assert.True(clock.Elapsed<TimeSpan.FromMilliseconds(700));
        }
        finally { exporter.Release.Set(); agent.Dispose(); }
    }
    [Fact]
    public async Task ExporterCallbacksCannotReenterFlushOrDeadlockShutdown()
    {
        var exporter=new CallbackExporter(); var agent=new Agent(Config(exporter)); var callbackFinished=new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        exporter.Callback=()=> { bool flushed=agent.Flush(1000); agent.Dispose(); callbackFinished.TrySetResult(flushed); };
        using(var span=TraceAIAuto.Source.StartActivity("reentrant")) Assert.NotNull(span);
        _=Task.Run(()=>agent.Flush(1000));
        try { Assert.False(await callbackFinished.Task.WaitAsync(TimeSpan.FromMilliseconds(700))); }
        finally { agent.Dispose(); }
    }
    [Fact]
    public void InitOnceDoesNotRestartAfterShutdownOrOwnCallerExporter()
    {
        var exporter=new CallbackExporter();
        try
        {
            void Configure(AutoOptions o) { o.ApiKey="explicit-api-key"; o.SecretKey="explicit-secret-key"; o.Exporter=exporter; }
            TraceAIAuto.Init(Configure); TraceAIAuto.Init(Configure); TraceAIAuto.Init();
            using(var span=TraceAIAuto.Source.StartActivity("once")) Assert.NotNull(span);
            Assert.True(TraceAIAuto.Flush(1000)); Assert.Equal(1,exporter.Exports); Assert.Equal(0,exporter.Shutdowns);
            TraceAIAuto.Shutdown(); Assert.Equal(0,exporter.Shutdowns);
            TraceAIAuto.Init(_=>throw new InvalidOperationException("second configure must not run"));
            using(var span=TraceAIAuto.Source.StartActivity("again")) Assert.Null(span);
            Assert.True(TraceAIAuto.Flush(1000)); Assert.Equal(1,exporter.Exports);
        }
        finally { TraceAIAuto.Shutdown(); }
    }
    private sealed class BlockingShutdownExporter : BaseExporter<Activity>
    {
        internal readonly ManualResetEventSlim Release=new(); internal readonly ManualResetEventSlim Entered=new();
        public override ExportResult Export(in Batch<Activity> batch) => ExportResult.Success;
        protected override bool OnShutdown(int timeoutMilliseconds) { Entered.Set(); Release.Wait(TimeSpan.FromSeconds(5)); return true; }
    }
    [Fact]
    public async Task ShutdownNeverWaitsOnCallerOwnedExporterOrHoldsInitGate()
    {
        var exporter=new BlockingShutdownExporter(); var agent=new Agent(Config(exporter));
        var shutdown=Task.Run(agent.Dispose);
        try { await shutdown.WaitAsync(TimeSpan.FromMilliseconds(700)); Assert.False(exporter.Entered.IsSet); }
        finally { exporter.Release.Set(); await shutdown; }
    }
}
