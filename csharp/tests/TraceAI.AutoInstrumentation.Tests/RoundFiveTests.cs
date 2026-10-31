using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.Loader;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFiveTests : InitIsolation
{
    private static AutoConfig Config(TraceConfig? mask=null,bool wire=true)=>new(new AutoOptions {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",TraceConfig=mask,WireCapture=wire,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    public static IEnumerable<object[]> EventCases=>from compatibility in new[] { false,true }
        from side in new[] { "input","output" } from flag in new[] { "all","messages","text" }
        select new object[] { compatibility,side,flag };
    [Theory]
    [MemberData(nameof(EventCases))]
    public async Task NativeMessageEventsObeyDirectionalHideFlagsOnActualOtlp(bool compatibility,string side,string flag)
    {
        var mask=new TraceConfig {
            HideInputs=side=="input"&&flag=="all",HideOutputs=side=="output"&&flag=="all",
            HideInputMessages=side=="input"&&flag=="messages",HideOutputMessages=side=="output"&&flag=="messages",
            HideInputText=side=="input"&&flag=="text",HideOutputText=side=="output"&&flag=="text" };
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config(mask));
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("Round5.MessageEvents");
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name).AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=source.StartActivity("message event privacy"))
        {
            foreach(string name in new[] { "gen_ai.user.message","gen_ai.system.message","gen_ai.tool.message","gen_ai.assistant.message","gen_ai.choice" })
            {
                string marker=name is "gen_ai.assistant.message" or "gen_ai.choice"?"OUTPUT_EVENT_PRIVATE":"INPUT_EVENT_PRIVATE";
                span!.AddEvent(new ActivityEvent(name,tags:new ActivityTagsCollection {
                    ["content"]=new[] { marker },["message"]="{\"content\":\""+marker+"\",\"tool_calls\":[{\"function\":{\"arguments\":\""+marker+"\"}}]}",
                    ["tool_calls.0.function.arguments"]=marker,["tool_call_id"]=marker }));
            }
        }
        byte[] bytes=Assert.Single(collector.Exports); string actual=Encoding.UTF8.GetString(bytes);
        Assert.DoesNotContain(side=="input"?"INPUT_EVENT_PRIVATE":"OUTPUT_EVENT_PRIVATE",actual);
        Assert.Contains(side=="input"?"OUTPUT_EVENT_PRIVATE":"INPUT_EVENT_PRIVATE",actual);
        Assert.Equal(5,Otlp.Extras(bytes,11).Count());
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task ExportTimeUrlsUseOneCredentialVocabularyOnEverySurface(bool compatibility)
    {
        string[] names=["client_secret","authorization_token","password","key","api_key","x-amz-signature","Ocp-Apim-Subscription-Key","x-goog-api-key","private_key","%63lient_secret"];
        const string secret="URL_CREDENTIAL_SECRET_9876";
        var tags=new ActivityTagsCollection(names.Select((name,i)=>new KeyValuePair<string,object?>("provider.url."+i,new[] { "https://provider.test/path?"+name+"="+secret+"&model=public" })));
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config());
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("Round5.UrlBoundary",tags:tags);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(tags.Select(p=>new KeyValuePair<string,object>(p.Key,p.Value!))))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=source.StartActivity("credential URLs",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),tags) }))
        {
            foreach(var tag in tags) span!.SetTag(tag.Key,tag.Value);
            span!.SetStatus(ActivityStatusCode.Error,"https://provider.test/?password="+secret);
            span.AddEvent(new ActivityEvent("https://provider.test/?authorization_token="+secret,tags:tags));
        }
        var bytes=Assert.Single(collector.Exports); Assert.DoesNotContain(secret,Encoding.UTF8.GetString(bytes));
        Assert.Contains("model=public",Encoding.UTF8.GetString(bytes));
        foreach(string name in names) Assert.Contains(secret,WirePrivacy.QuerySecrets("https://provider.test/?"+name+"="+secret));
        foreach(string header in new[] { "Authorization","x-api-key","api-key","xi-api-key","x-goog-api-key","anthropic-api-key","Ocp-Apim-Subscription-Key","x-amz-security-token" })
            Assert.Equal(TraceConfig.RedactedValue,privacy.Mask("http.request.header."+header+".0",secret));
    }
    [Fact]
    public void RegistryIsBoundedLruLongestFirstAndSnapshotsAreImmutable()
    {
        TraceAIAuto.RegisterSecret("short"); Assert.Empty(KnownSecrets.Snapshot());
        for(int i=0;i<KnownSecrets.Capacity;i++) TraceAIAuto.RegisterSecret("CREDENTIAL_VALUE_"+i.ToString("D4"));
        var before=KnownSecrets.Snapshot(); Assert.Equal(1024,before.Length);
        TraceAIAuto.RegisterSecret("CREDENTIAL_VALUE_0000"); TraceAIAuto.RegisterSecret("CREDENTIAL_VALUE_0000_LONGER");
        var after=KnownSecrets.Snapshot(); Assert.Equal(1024,after.Length);
        Assert.Contains("CREDENTIAL_VALUE_0000",after); Assert.DoesNotContain("CREDENTIAL_VALUE_0001",after);
        Assert.Contains("CREDENTIAL_VALUE_0001",before); Assert.Equal("CREDENTIAL_VALUE_0000_LONGER",after[0]);
        Assert.Equal("<redacted>",WirePrivacy.ScrubString("CREDENTIAL_VALUE_0000_LONGER",after));
        Assert.DoesNotContain("CREDENTIAL_VALUE",TraceAIAuto.Doctor());
    }
    [Fact]
    public void RegistryIsSafeUnderConcurrentRegistrationAndExportLookups()
    {
        Parallel.For(0,4096,i=> { TraceAIAuto.RegisterSecret("CONCURRENT_SECRET_"+i.ToString("D4")); Assert.InRange(KnownSecrets.Snapshot().Length,1,1024); });
        Assert.Equal(1024,KnownSecrets.Snapshot().Distinct().Count());
    }
    private sealed class OtherContext() : AssemblyLoadContext(isCollectible:true)
    {
        protected override Assembly? Load(AssemblyName name)=>Default.Assemblies.FirstOrDefault(a=>a.GetName().Name==name.Name);
    }
    [Fact]
    public void PublicRegistrationUsesTheSameRegistryAcrossIsolatedAndDefaultLoadContexts()
    {
        var context=new OtherContext();
        try
        {
            var entry=context.LoadFromAssemblyPath(typeof(TraceAIAuto).Assembly.Location).GetType(typeof(TraceAIAuto).FullName!)!;
            entry.GetMethod(nameof(TraceAIAuto.RegisterSecret))!.Invoke(null,new object[] { "SECOND_CONTEXT_SECRET_9876" });
            Assert.Contains("SECOND_CONTEXT_SECRET_9876",KnownSecrets.Snapshot());
            TraceAIAuto.RegisterSecret("DEFAULT_CONTEXT_SECRET_9876");
            var registry=entry.Assembly.GetType(typeof(KnownSecrets).FullName!)!;
            var snapshot=(string[])registry.GetMethod("Snapshot",BindingFlags.Static|BindingFlags.NonPublic)!.Invoke(null,null)!;
            Assert.Contains("DEFAULT_CONTEXT_SECRET_9876",snapshot);
        }
        finally { context.Unload(); }
    }
    [Fact]
    public void RegistrySeedsCredentialLikeEnvironmentExactlyAtInitialization()
    {
        string[] names=["ROUND5_API_KEY","ROUND5_TOKEN","ROUND5_SECRET","ROUND5_PASSWORD","ROUND5_PASSWD","ROUND5_CREDENTIAL","ROUND5_AUTH","ROUND5_ORDINARY"];
        var old=names.ToDictionary(name=>name,Environment.GetEnvironmentVariable);
        try
        {
            for(int i=0;i<names.Length;i++) Environment.SetEnvironmentVariable(names[i],"ENV_VALUE_"+i.ToString("D4"));
            _=new AutoProcessor(Config()); var values=KnownSecrets.Snapshot();
            for(int i=0;i<names.Length-1;i++) Assert.Contains("ENV_VALUE_"+i.ToString("D4"),values);
            Assert.DoesNotContain("ENV_VALUE_0007",values); Assert.Contains("explicit-api-key",values);
            Environment.SetEnvironmentVariable(names[0],"LATE_ENV_SECRET_9876"); _=new AutoProcessor(Config());
            Assert.DoesNotContain("LATE_ENV_SECRET_9876",KnownSecrets.Snapshot());
        }
        finally { foreach(var pair in old) Environment.SetEnvironmentVariable(pair.Key,pair.Value); }
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task RegisteredCredentialsScrubUnrelatedSpansOnEveryActualOtlpSurface(bool compatibility)
    {
        const string secret="PROCESS_WIDE_SECRET_9876";
        // The activity starts before registration, and has no relationship to the request.
        await using var collector=new MockServer([],"application/json");
        var privacy=new AutoProcessor(Config());
        var tags=new ActivityTagsCollection { ["note"]=new[] { secret } };
        using var source=new ActivitySource("Round5."+secret,secret,tags:tags);
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string,object> { ["note"]=new[] { secret } }))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using(var span=source.StartActivity(secret,ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"vendor="+secret),tags) }))
        {
            TraceAIAuto.RegisterSecret(secret);
            span!.TraceStateString="vendor="+secret; span.SetTag("note",new[] { secret }); span.SetTag(secret,secret);
            span.SetStatus(ActivityStatusCode.Error,secret); span.AddEvent(new ActivityEvent(secret,tags:tags));
        }
        byte[] bytes=Assert.Single(collector.Exports); Assert.DoesNotContain(secret,Encoding.UTF8.GetString(bytes));
        Assert.Equal(secret,source.Version); // Another exporter still sees original values.
        using(var later=source.StartActivity("later")) later!.SetTag("unrelated",secret);
        Assert.All(collector.Exports,bytes=>Assert.DoesNotContain(secret,Encoding.UTF8.GetString(bytes)));
    }
    [Theory]
    [InlineData("gzip")] [InlineData("br")] [InlineData("deflate")] [InlineData("zlib")] [InlineData("identity")]
    public async Task SuppressedUnknownEndpointObservesBufferedCredentialsWithoutMaterializingContent(string coding)
    {
        byte[] encoded=Encode("{\"mcp_servers\":[{\"authorization_token\":\"BUFFERED_BODY_SECRET_9876\"}]}",coding);
        using var content=new ByteArrayContent(encoded); if(coding!="identity") content.Headers.ContentEncoding.Add(coding=="zlib"?"deflate":coding);
        content.Headers.ContentType=new("application/json");
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://unknown.test/unrelated?client_secret=QUERY_OBSERVED_SECRET_9876") { Content=content };
        request.Headers.TryAddWithoutValidation("Authorization","Bearer HEADER_OBSERVED_SECRET_9876");
        using var capture=new HttpCapture(Config(wire:false));
        using var suppression=SuppressInstrumentationScope.Begin();
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        var values=KnownSecrets.Snapshot();
        Assert.Contains("BUFFERED_BODY_SECRET_9876",values); Assert.Contains("QUERY_OBSERVED_SECRET_9876",values); Assert.Contains("HEADER_OBSERVED_SECRET_9876",values);
        Assert.Same(content,request.Content); Assert.Equal(encoded,await content.ReadAsByteArrayAsync());
    }
    public static IEnumerable<object[]> DecodeFailures=>from coding in new[] { "gzip","br","deflate","zlib" }
        from shape in new[] { "half","trailer","limit" } select new object[] { coding,shape };
    [Theory]
    [MemberData(nameof(DecodeFailures))]
    public async Task IncompleteBufferedDecodingDoesNotPoisonUnrelatedSpansOrChangeRequest(string coding,string shape)
    {
        byte[] encoded=Encode("{\"messages\":[],\"padding\":\""+new string('x',1024)+"\",\"token\":\"LATE_BODY_SECRET_9876\"}",coding);
        if(shape=="half") encoded=encoded[..(encoded.Length/2)];
        if(shape=="trailer") encoded=encoded[..^1];
        using var content=new ByteArrayContent(encoded); content.Headers.ContentType=new("application/json"); content.Headers.ContentEncoding.Add(coding=="zlib"?"deflate":coding);
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://unknown.test/unrelated") { Content=content };
        var options=new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",WireCapture=false,WireMaxBodyBytes=shape=="limit"?128:4194304,
            ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") };
        var config=new AutoConfig(options); using var capture=new HttpCapture(config);
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        Assert.NotNull(KnownSecrets.Snapshot());
        using var original=new Activity("unrelated later call").Start(); original.SetTag("output.value","unrelated answer"); original.SetStatus(ActivityStatusCode.Error,"later failure"); original.Stop();
        var batch=new Batch<Activity>([original],1); var safe=Assert.Single(new AutoProcessor(config).PrepareExport(in batch,Resource.Empty).Spans);
        Assert.Equal("unrelated answer",safe.GetTagItem("output.value")); Assert.Equal("later failure",safe.StatusDescription); Assert.Equal(original.TraceId,safe.TraceId); Assert.Equal(original.Duration,safe.Duration);
        Assert.Same(content,request.Content); Assert.Equal(encoded,await content.ReadAsByteArrayAsync());
    }
    [Theory]
    [InlineData("string")] [InlineData("memory")] [InlineData("form")] [InlineData("multipart")]
    public void BufferedBclContentShapesRegisterCredentialsWithoutReadingOrReplacingContent(string shape)
    {
        byte[] json=Encoding.UTF8.GetBytes("{\"client_secret\":\"BCL_BUFFERED_SECRET_9876\"}");
        using HttpContent content=shape switch {
            "string"=>new StringContent(Encoding.UTF8.GetString(json)),
            "memory"=>new ReadOnlyMemoryContent(json),
            "form"=>new FormUrlEncodedContent(new Dictionary<string,string> { ["client_secret"]="BCL_BUFFERED_SECRET_9876" }),
            _=>new MultipartFormDataContent { { new StringContent("BCL_BUFFERED_SECRET_9876"),"client_secret" } } };
        using var request=new HttpRequestMessage(HttpMethod.Post,"https://unknown.test/other") { Content=content };
        using var capture=new HttpCapture(Config(wire:false));
        capture.OnNext(new KeyValuePair<string,object?>("System.Net.Http.HttpRequestOut.Start",new { Request=request }));
        Assert.Contains("BCL_BUFFERED_SECRET_9876",KnownSecrets.Snapshot()); Assert.Same(content,request.Content);
        // These fields would be populated by HttpContent.ReadAs*; observation must
        // leave both the encoded-body buffer and cached read stream untouched.
        foreach(var field in typeof(HttpContent).GetFields(BindingFlags.Instance|BindingFlags.NonPublic).Where(field=>field.Name is "_bufferedContent" or "_contentReadStream"))
            Assert.Null(field.GetValue(content));
    }
    [Fact]
    public void RegisteredCredentialsNeverAppearInDoctorEvenUnderNonCredentialConfigurationKeys()
    {
        TraceAIAuto.RegisterSecret("DIAGNOSTIC_ECHO_SECRET_9876");
        TraceAIAuto.Init(options=> { options.ApiKey="explicit-api-key"; options.SecretKey="explicit-secret-key"; options.ProjectName="DIAGNOSTIC_ECHO_SECRET_9876"; options.Exporter=new MemoryExporter(); });
        Assert.DoesNotContain("DIAGNOSTIC_ECHO_SECRET_9876",TraceAIAuto.Doctor());
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookSharesPublicRegistrationWithDefaultContextAndSeedsEnvironment(string framework)
    {
        await using var server=new MockServer([],"application/json");
        var child=await StartupHookTests.RunApp(server.Url,"round5_registration",new Dictionary<string,string?> {
            ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url,["ROUND5_PROVIDER_AUTH"]="ENV_CREDENTIAL_SECRET_9876" },framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error); Assert.Equal("registration-complete",child.Output.Trim());
        byte[] bytes=Assert.Single(server.Exports); Assert.Single(Otlp.Spans(bytes)); string actual=Encoding.UTF8.GetString(bytes);
        Assert.DoesNotContain("PUBLIC_API_REGISTERED_SECRET_9876",actual); Assert.DoesNotContain("ENV_CREDENTIAL_SECRET_9876",actual);
        Assert.Contains("<redacted>",actual);
    }
    [Fact]
    public void RegisteredValuesAlsoScrubStringsProducedByNonFiniteArrayConversion()
    {
        TraceAIAuto.RegisterSecret("Infinity");
        using var original=new Activity("nonfinite credentials").Start(); original.SetTag("numbers",new[] { double.PositiveInfinity,double.NegativeInfinity }); original.Stop();
        var batch=new Batch<Activity>([original],1); var safe=Assert.Single(new AutoProcessor(Config()).PrepareExport(in batch,Resource.Empty).Spans);
        var values=Assert.IsType<object[]>(safe.GetTagItem("numbers"));
        Assert.Equal(new object[] { "<redacted>","-<redacted>" },values);
    }
    [Fact]
    public void FiKeysAreRefreshedAtExportEvenAfterLruChurn()
    {
        var privacy=new AutoProcessor(Config());
        for(int i=0;i<1024;i++) TraceAIAuto.RegisterSecret("CHURN_SECRET_"+i.ToString("D4"));
        Assert.DoesNotContain("explicit-api-key",KnownSecrets.Snapshot());
        using var original=new Activity("FI churn").Start();
        original.SetStatus(ActivityStatusCode.Error,"explicit-api-key explicit-secret-key");
        original.AddEvent(new ActivityEvent("explicit-api-key",tags:new ActivityTagsCollection { ["note"]=new[] { "explicit-secret-key" } })); original.Stop();
        var batch=new Batch<Activity>([original],1); var safe=Assert.Single(privacy.PrepareExport(in batch,Resource.Empty).Spans);
        Assert.Equal("<redacted> <redacted>",safe.StatusDescription); Assert.Equal("<redacted>",Assert.Single(safe.Events).Name);
        Assert.Equal(1024,KnownSecrets.Snapshot().Length);
    }
    [Theory]
    [InlineData("x-secret-key")] [InlineData("proxy-authorization")] [InlineData("x-auth-token")]
    [InlineData("x-access-token")] [InlineData("client-secret")] [InlineData("api-token")]
    public void EveryCredentialHeaderCollectorNameAlsoMasksIndexedAttributes(string header)
    {
        const string secret="NAMED_HEADER_SECRET_9876";
        string value=header=="proxy-authorization"?"Bearer "+secret:secret;
        Assert.Contains(secret,WirePrivacy.HeaderSecrets(new Dictionary<string,string> { [header]=value }));
        Assert.Equal(TraceConfig.RedactedValue,new AutoProcessor(Config()).Mask("http.request.header."+header+".0",new[] { secret }));
    }
    [Fact]
    public void StructuralMediaIncludesIndexedAudioAndSmallMimeTaggedData()
    {
        var privacy=new AutoProcessor(Config());
        using var original=new Activity("media").Start();
        original.SetTag("input.audio.0","QUJD");
        original.SetTag("input.parts.0.inlineData.data","QUJD"); original.SetTag("input.parts.0.inlineData.mimeType","audio/wav");
        original.SetTag("output.value","{\"inlineData\":{\"mimeType\":\"image/png\",\"data\":\"QUJD\"}}"); original.Stop();
        var batch=new Batch<Activity>([original],1); var safe=Assert.Single(privacy.PrepareExport(in batch,Resource.Empty).Spans);
        Assert.DoesNotContain("QUJD",string.Join(" ",safe.TagObjects.Select(pair=>pair.Value)));
        Assert.Equal("QUJD",original.GetTagItem("input.audio.0"));
    }
    internal static byte[] Encode(string text,string coding)
    {
        byte[] bytes=Encoding.UTF8.GetBytes(text); if(coding=="identity") return bytes;
        using var memory=new MemoryStream();
        using(Stream compressor=coding switch { "gzip"=>new GZipStream(memory,CompressionLevel.Fastest,true),"br"=>new BrotliStream(memory,CompressionLevel.Fastest,true),"zlib"=>new ZLibStream(memory,CompressionLevel.Fastest,true),_=>new DeflateStream(memory,CompressionLevel.Fastest,true) }) compressor.Write(bytes);
        return memory.ToArray();
    }
    public static IEnumerable<object[]> StartupCases=>from framework in new[] { "net8.0","net10.0" }
        from coding in new[] { "gzip","br","deflate" } from layer in new[] { "wire","library","privacy_only" }
        select new object[] { framework,coding,layer };
    [Theory]
    [MemberData(nameof(StartupCases))]
    public async Task RealHookCompressedCredentialRegistryProtectsLaterUnrelatedSpansWithBaselineParity(string framework,string coding,string layer)
    {
        byte[] body=Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"bad COMPRESSED_MCP_TOKEN_9876\"}}");
        await using var server=new MockServer(body,"application/json") { StatusCode=401 };
        var environment=new Dictionary<string,string?> { ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
        if(layer=="privacy_only") environment["FI_WIRE_CAPTURE"]="false";
        var baselineEnvironment=new Dictionary<string,string?>(environment) { ["DOTNET_STARTUP_HOOKS"]=null };
        string mode="round5_compressed_"+coding+"_"+layer;
        var baseline=await StartupHookTests.RunApp(server.Url+"v1/messages",mode,baselineEnvironment,framework);
        var traced=await StartupHookTests.RunApp(server.Url+"v1/messages",mode,environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error); Assert.Equal(baseline.Output,traced.Output);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error);
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(2,spans.Length);
        Assert.All(server.Exports,bytes=>Assert.DoesNotContain("COMPRESSED_MCP_TOKEN_9876",Encoding.UTF8.GetString(bytes)));
        Assert.Contains(spans,span=>span.Attributes.TryGetValue("later.echo",out var value)&&Equals(value,"<redacted>"));
    }
    [Theory]
    [InlineData("net8.0","all")] [InlineData("net10.0","all")]
    [InlineData("net8.0","messages")] [InlineData("net10.0","messages")]
    public async Task RealHookHidesNativeMessageEventsAndCredentialUrls(string framework,string flag)
    {
        await using var server=new MockServer([],"application/json");
        var environment=new Dictionary<string,string?> { ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url,
            [flag=="all"?"FI_HIDE_INPUTS":"FI_HIDE_INPUT_MESSAGES"]="true",[flag=="all"?"FI_HIDE_OUTPUTS":"FI_HIDE_OUTPUT_MESSAGES"]="true" };
        var child=await StartupHookTests.RunApp(server.Url,"round5_events",environment,framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error); Assert.Equal("events-complete",child.Output.Trim());
        Assert.Single(server.Exports.SelectMany(Otlp.Spans));
        Assert.All(server.Exports,bytes=> { string actual=Encoding.UTF8.GetString(bytes); Assert.DoesNotContain("PRIVATE_EVENT_CONTENT",actual); Assert.DoesNotContain("URL_UNDISCOVERED_SECRET_9876",actual); });
    }
}
