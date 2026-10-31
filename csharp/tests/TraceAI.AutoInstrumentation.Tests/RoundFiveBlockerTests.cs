using System.Diagnostics;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFiveBlockerTests : InitIsolation
{
    private static AutoConfig Config()=>new(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    private static Dictionary<string,string?> EnvironmentFor(MockServer server)=>new() {
        ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
    [Theory]
    [InlineData("bytes")] [InlineData("string")] [InlineData("memory")]
    public async Task BufferedMultipartInspectionDoesNotMaterializePartLengths(string shape)
    {
        byte[] bytes="multipart-file-bytes"u8.ToArray();
        using HttpContent file=shape switch { "string"=>new StringContent(Encoding.UTF8.GetString(bytes)),
            "memory"=>new ReadOnlyMemoryContent(bytes),_=>new ByteArrayContent(bytes) };
        using var content=new MultipartFormDataContent("traceai-length-regression") { { new StringContent("whisper-1"),"model" },{ file,"file","clip.wav" } };
        // HttpClient has computed the enclosing length before its Start callback.
        long advertised=content.Headers.ContentLength!.Value;
        using var before=new MemoryStream(); await content.CopyToAsync(before);
        Assert.False(file.Headers.Contains("Content-Length"));
        var request=new RequestData(); HttpCapture.ReadRequest(content,request,4194304);
        using var after=new MemoryStream(); await content.CopyToAsync(after);
        Assert.False(file.Headers.Contains("Content-Length"));
        Assert.Equal(before.ToArray(),after.ToArray()); Assert.Equal(advertised,after.Length);
        Assert.Equal(bytes.Length,Assert.Single(request.Files).Size);
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task StructuralAudioAndEveryDataUriAreRemovedOnActualOtlpSurfaces(bool compatibility)
    {
        const string audio="{\"audio_base64\":\"QUJDREVG\",\"audio\":{\"bytes\":\"R0hJSktM\",\"transcript\":\"spoken words\",\"format\":\"wav\"},\"input_audio\":[\"MQ==\",{\"data\":\"Mg==\"}],\"output.audio.0.bytes\":\"Mw==\",\"bytes\":\"ordinary\"}";
        string[] uris=["data:audio/wav,%53%54%55%56%57%58","data:text/plain;charset=utf-8,RAW_MEDIA_PAYLOAD","data:,%01%02%03","data:audio/wav;base64,QUJD%52%45VG"];
        var tags=new ActivityTagsCollection { ["record"]=audio,["uris"]=uris };
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config());
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("Round5.StructuralMedia",tags:tags);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(tags.Select(pair=>new KeyValuePair<string,object>(pair.Key,pair.Value!))))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=source.StartActivity("structural media",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"vendor="+uris[0]),tags) }))
        {
            foreach(var pair in tags) span!.SetTag(pair.Key,pair.Value);
            span!.SetTag("input.audio.0.bytes","NA=="); span.SetStatus(ActivityStatusCode.Error,audio);
            span.TraceStateString="vendor="+uris[0]; span.AddEvent(new ActivityEvent(uris[0],tags:tags));
        }
        byte[] exported=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(exported);
        foreach(string payload in new[] { "QUJDREVG","R0hJSktM","MQ==","Mg==","Mw==","NA==","%53%54%55%56%57%58","RAW_MEDIA_PAYLOAD","%01%02%03","QUJD%52%45VG" })
            Assert.DoesNotContain(payload,actual);
        var safe=Assert.Single(Otlp.Spans(exported)); string record=Assert.IsType<string>(safe.Attributes["record"]);
        Assert.Contains("spoken words",record); Assert.Contains("ordinary",record); Assert.Contains("wav",record);
        Assert.Equal("<omitted: 4 base64 chars>",safe.Attributes["input.audio.0.bytes"]);
        Assert.Equal(audio,source.Tags!.First(pair=>pair.Key=="record").Value); // Snapshot-only redaction.
    }
    [Fact]
    public async Task MultipartSerializationRegistersEveryParsedCredentialBeforeAnyResponseOrSpan()
    {
        using var content=new MultipartFormDataContent("traceai-observed-credentials") {
            { new StringContent("whisper-1"),"model" },
            { new StreamContent(new MemoryStream("FIRST_BODY_CREDENTIAL_9876"u8.ToArray())),"client_secret" },
            { new StreamContent(new MemoryStream("SECOND_BODY_CREDENTIAL_9876"u8.ToArray())),"client_secret" },
            { new StreamContent(new MemoryStream("{\"authorization_token\":\"NESTED_BODY_CREDENTIAL_9876\"}"u8.ToArray())),"metadata" } };
        var request=new RequestData(); var observer=MultipartObserver.Create(content,request)!;
        using var sent=new MemoryStream(); await content.CopyToAsync(sent); var bytes=sent.ToArray();
        for(int i=0;i<bytes.Length;i+=7) observer.Bytes(bytes.AsSpan(i,Math.Min(7,bytes.Length-i)));
        Assert.False(request.BodyUnavailable); Assert.Equal("SECOND_BODY_CREDENTIAL_9876",request.Fields!["client_secret"]);
        foreach(string value in new[] { "FIRST_BODY_CREDENTIAL_9876","SECOND_BODY_CREDENTIAL_9876","NESTED_BODY_CREDENTIAL_9876" }) Assert.Contains(value,KnownSecrets.Snapshot());
    }
    [Fact]
    public void WireCorePublishesCredentialsDiscoveredByItsMultipartParser()
    {
        var request=new RequestData { Url="https://api.openai.com/v1/audio/transcriptions",
            Headers=new(StringComparer.OrdinalIgnoreCase) { ["content-type"]="multipart/form-data; boundary=probe" },
            Body=Encoding.UTF8.GetBytes("--probe\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--probe\r\nContent-Disposition: form-data; name=\"client_secret\"\r\n\r\nPARSED_BODY_CREDENTIAL_9876\r\n--probe--\r\n") };
        Assert.NotNull(WireCore.Build(WireCore.Match("POST",request.Url)!,request,new ResponseData { Status=200 }));
        Assert.Contains("PARSED_BODY_CREDENTIAL_9876",KnownSecrets.Snapshot());
    }
    [Fact]
    public void MultipartCredentialDiscoveryNeverExaminesParsedFilePayloads()
    {
        var request=new RequestData { Body=new byte[5*1024*1024],Fields=new() { ["model"]="whisper-1",["client_secret"]="TEXT_FIELD_CREDENTIAL_9876" } };
        KnownSecrets.Register(WirePrivacy.RequestSecrets(request));
        Assert.Equal("TEXT_FIELD_CREDENTIAL_9876",Assert.Single(KnownSecrets.Snapshot()));
    }
    public static IEnumerable<object[]> UploadCases=>from framework in new[] { "net8.0","net10.0" }
        from send in new[] { "sync","async" } select new object[] { framework,send };
    [Theory]
    [MemberData(nameof(UploadCases))]
    public async Task RealHookBufferedMultipartUploadsPreserveBytesAndPartHeaders(string framework,string send)
    {
        await using var server=new MockServer("{\"text\":\"transcribed\"}"u8.ToArray(),"application/json");
        var environment=EnvironmentFor(server); string mode="round5_multipart_buffered_"+send;
        var baseline=await StartupHookTests.RunApp(server.Url+"v1/audio/transcriptions",mode,new(environment) { ["DOTNET_STARTUP_HOOKS"]=null },framework);
        var traced=await StartupHookTests.RunApp(server.Url+"v1/audio/transcriptions",mode,environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        Assert.Contains("file_header_before=False;file_header_after=False",traced.Output);
        var bodies=server.RequestBodies.ToArray(); Assert.Equal(2,bodies.Length); Assert.Equal(bodies[0],bodies[1]);
        foreach(var headers in server.RequestHeaders) Assert.Equal(bodies[0].Length.ToString(),headers["Content-Length"]);
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans)); Assert.Equal("wire",span.Attributes["traceai.capture.layer"]);
        Assert.Equal("multipart-file-bytes"u8.Length.ToString(),span.Attributes["traceai.audio.input_bytes"].ToString());
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookObservedMultipartCredentialsProtectUnrelatedLaterSpans(string framework)
    {
        await using var server=new MockServer("{\"text\":\"transcribed\"}"u8.ToArray(),"application/json");
        var environment=EnvironmentFor(server); const string mode="round5_multipart_stream_credentials";
        var baseline=await StartupHookTests.RunApp(server.Url+"v1/audio/transcriptions",mode,new(environment) { ["DOTNET_STARTUP_HOOKS"]=null },framework);
        var traced=await StartupHookTests.RunApp(server.Url+"v1/audio/transcriptions",mode,environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(2,spans.Length);
        var later=Assert.Single(spans,span=>span.Attributes.ContainsKey("later.echo")); Assert.Equal("<redacted>",later.Attributes["later.echo"]);
        Assert.All(server.Exports,bytes=>Assert.DoesNotContain("BODY_CREDENTIAL_9876",Encoding.UTF8.GetString(bytes)));
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookRemovesShortAudioAndNonBase64DataUrisWithBaselineParity(string framework)
    {
        await using var server=new MockServer([],"application/json"); var environment=EnvironmentFor(server);
        var baseline=await StartupHookTests.RunApp(server.Url,"round5_structural_media",new(environment) { ["DOTNET_STARTUP_HOOKS"]=null },framework);
        var traced=await StartupHookTests.RunApp(server.Url,"round5_structural_media",environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        byte[] bytes=Assert.Single(server.Exports); Assert.Single(Otlp.Spans(bytes)); string actual=Encoding.UTF8.GetString(bytes);
        Assert.DoesNotContain("QUJDREVG",actual); Assert.DoesNotContain("R0hJSktM",actual); Assert.DoesNotContain("%53%54%55%56%57%58",actual);
        Assert.Contains("spoken words",actual);
    }
}
