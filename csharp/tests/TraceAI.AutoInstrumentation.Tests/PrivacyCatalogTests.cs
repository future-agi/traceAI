using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class PrivacyCatalogTests : InitIsolation
{
    private static string Shipping(string name)=>Path.Combine(WireFixtureTests.Repo,"csharp/src/TraceAI.AutoInstrumentation/Wire",name);
    private static AutoConfig Config()=>new(new AutoOptions { ApiKey="explicit-api-key",SecretKey="explicit-secret-key",ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp/absent.toml") });
    [Fact]
    public void ShippingPrivacyCatalogAndFixturesAreByteIdenticalAndCatalogIsEmbedded()
    {
        foreach(string name in new[] { "privacy.json","privacy-fixtures.json" })
            Assert.Equal(File.ReadAllBytes(Path.Combine(WireFixtureTests.Repo,"spec/auto-instrumentation",name)),File.ReadAllBytes(Shipping(name)));
        using var resource=typeof(WirePrivacy).Assembly.GetManifestResourceStream("TraceAI.AutoInstrumentation.Wire.privacy.json")!;
        using var memory=new MemoryStream(); resource.CopyTo(memory);
        Assert.Equal(File.ReadAllBytes(Shipping("privacy.json")),memory.ToArray());
    }
    private static JsonNode? Actual(JsonNode value,string path,Dictionary<string,PrivacyCatalog.Hint>? hints,string[] secrets,bool framed)
    {
        if(value is JsonObject or JsonArray)
        {
            string clean=(string)WirePrivacy.Scrub(value,secrets,path,hints,framed);
            return clean.StartsWith('<')?JsonValue.Create(clean):JsonNode.Parse(clean);
        }
        if(WireCore.S(value) is {} text) return JsonValue.Create(WirePrivacy.ScrubString(text,secrets,path,hints,framed));
        object scalar=value.GetValueKind()==JsonValueKind.Number?value.GetValue<double>():value.GetValue<bool>();
        return JsonSerializer.SerializeToNode(WirePrivacy.Scrub(scalar,secrets,path,hints,framed));
    }
    [Fact]
    public void EverySpecPrivacyFixturePassesProductionBoundaryAndIsIdempotent()
    {
        var cases=JsonNode.Parse(File.ReadAllText(Shipping("privacy-fixtures.json")))!["cases"]!.AsArray();
        Assert.True(cases.Count>=4985);
        var failures=new List<string>();
        foreach(var fixture in cases)
        {
            var input=fixture!["input"]!; string path=input["path"]!.GetValue<string>();
            var siblings=input["siblings"] as JsonObject;
            var hints=siblings==null?null:PrivacyCatalog.IndexHints(siblings.Select(p=>new KeyValuePair<string,object?>(p.Key,p.Value)));
            var secrets=(input["known_secrets"] as JsonArray)?.Select(n=>n!.GetValue<string>()).ToArray()??[];
            bool framed=input["site"]!.GetValue<string>()!="free_text_urls";
            try
            {
                JsonNode? actual=Actual(input["value"]!,path,hints,secrets,framed);
                if(!JsonNode.DeepEquals(fixture["expected"],actual))
                    failures.Add(fixture["name"]+": expected "+fixture["expected"]!.ToJsonString()+" actual "+actual?.ToJsonString());
                else if(actual!=null&&!JsonNode.DeepEquals(actual,Actual(actual,path,hints,secrets,framed))) failures.Add(fixture["name"]+": non-idempotent");
            }
            catch(Exception error) { failures.Add(fixture["name"]+": "+error.GetType().Name); }
        }
        Assert.True(failures.Count==0,$"{failures.Count}/{cases.Count} failures\n"+string.Join('\n',failures.Take(30)));
    }
    [Theory]
    [InlineData("key")] [InlineData("passwd")] [InlineData("x-vault-token")]
    [InlineData("Set-Cookie.0")] [InlineData("nested.provider_credentials[0].value")]
    public void CredentialHarvestingRetainsPathsAndContainerAncestry(string key)
    {
        const string secret="S6_BODY_CREDENTIAL_9876";
        var body=new JsonObject { [key]=new JsonArray(new JsonObject { ["chunks"]=new JsonArray(secret) }) };
        var request=new RequestData { Body=Encoding.UTF8.GetBytes(body.ToJsonString()) };
        Assert.Contains(secret,WirePrivacy.RequestSecrets(request));
        Assert.Contains(secret,WirePrivacy.HeaderSecrets(new Dictionary<string,string> { [key]=secret }));
        Assert.Contains(secret,WirePrivacy.QuerySecrets("https://example.invalid/?"+Uri.EscapeDataString(key)+"="+secret));
    }
    [Fact]
    public void CredentialDiscoveryAndExportShareAllCatalogPatternsAndExclusions()
    {
        const string secret="S6_TRANSPORT_SECRET_9876";
        var catalog=JsonNode.Parse(File.ReadAllText(Shipping("privacy.json")))!;
        var names=catalog["credentials"]!["patterns"]!.AsArray().Select(n=>n!["example"]!.GetValue<string>())
            .Concat(catalog["credentials"]!["seed_names"]!.AsArray().Select(n=>n!.GetValue<string>()));
        foreach(string name in names)
        {
            Assert.True(WirePrivacy.CredentialKey(name),name);
            Assert.Contains(secret,WirePrivacy.HeaderSecrets(new Dictionary<string,string> { [name]=secret }));
            Assert.Contains(secret,WirePrivacy.QuerySecrets("https://example.invalid/?"+Uri.EscapeDataString(name)+"="+secret));
            Assert.Contains(secret,WirePrivacy.RequestSecrets(new RequestData { Body=Encoding.UTF8.GetBytes(new JsonObject { [name]=new JsonArray(secret) }.ToJsonString()) }));
        }
        foreach(var rule in catalog["credentials"]!["exclusions"]!.AsArray())
        foreach(string name in rule!["examples"]!.AsArray().Select(n=>n!.GetValue<string>()))
        {
            Assert.False(WirePrivacy.CredentialKey(name),name);
            Assert.Empty(WirePrivacy.HeaderSecrets(new Dictionary<string,string> { [name]=secret }));
            Assert.Empty(WirePrivacy.QuerySecrets("https://example.invalid/?"+Uri.EscapeDataString(name)+"="+secret));
            Assert.Empty(WirePrivacy.RequestSecrets(new RequestData { Body=Encoding.UTF8.GetBytes(new JsonObject { [name]=secret }.ToJsonString()) }));
        }
        Assert.Empty(WirePrivacy.RequestSecrets(new RequestData { Body="{\"choices\":[{\"logprobs\":{\"content\":[{\"token\":\"ordinary logprob text\"}]}}]}"u8.ToArray() }));
    }
    [Theory]
    [InlineData(false)] [InlineData(true)]
    public async Task AllActualOtlpSurfacesUseCatalogMediaCredentialsAndQueryContext(bool compatibility)
    {
        var tags=new ActivityTagsCollection {
            ["output.value"]="{\"mimeType\":\"audio/wav\",\"content\":\"QUJDREVG\",\"media_type\":\"audio/pcm\",\"value\":{\"chunk\":\"R0hJSktM\"}}",
            ["input.parts[0].content.chunk[0]"]="AQIDBA==",["input.parts[0].media_type[0]"]=new[] { "audio/pcm" },
            ["input.parts[1].content.chunk[0]"]="ordinary payload",["input.parts[0].transcript.content"]="spoken words",
            ["output.magic"]="iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",["input.audio.0"]=new[] { 1,2,3 },
            ["http.response.url"]="https://example.invalid/?mime_type=audio%2Fpcm&content=AQIDBA%3D%3D&private-token=S6_URL_SECRET_9876&model=keep#anchor",
            ["opaque.uri"]="data:image/svg+xml,<svg title='S6_URI_PAYLOAD'>literal payload</svg>",
            ["provider.Set-Cookie[0]"]="S6_COOKIE_SECRET_9876",["ordinary.max_tokens"]=123 };
        await using var collector=new MockServer([],"application/json"); var privacy=new AutoProcessor(Config());
        using var exporter=new TrackingOtlpExporter(new OtlpExporterOptions { Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf },privacy,new ExportCompletion(),compatibility);
        using var source=new ActivitySource("OpenAI.S6",tags:tags);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(tags.Select(p=>new KeyValuePair<string,object>(p.Key,p.Value!))))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using var original=source.StartActivity("catalog",ActivityKind.Client,default(ActivityContext),links:new[] {
            new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded),tags) })!;
        foreach(var pair in tags) original.SetTag(pair.Key,pair.Value);
        original.SetStatus(ActivityStatusCode.Error,tags["output.value"] as string); original.AddEvent(new ActivityEvent("response",tags:tags)); original.Stop();
        string exported=Encoding.UTF8.GetString(Assert.Single(collector.Exports));
        foreach(string value in new[] { "QUJDREVG","R0hJSktM","AQIDBA","iVBORw0KGgo","S6_URL_SECRET_9876","S6_URI_PAYLOAD","S6_COOKIE_SECRET_9876" }) Assert.DoesNotContain(value,exported);
        Assert.Contains("spoken words",exported); Assert.Contains("ordinary payload",exported); Assert.Contains("model=keep#anchor",exported);
        Assert.Equal(123,original.GetTagItem("ordinary.max_tokens")); Assert.Equal("S6_COOKIE_SECRET_9876",original.GetTagItem("provider.Set-Cookie[0]"));
    }
    [Theory]
    [InlineData("net8.0")] [InlineData("net10.0")]
    public async Task RealHookHarvestsCatalogNamesAndCredentialArraysUnderCoveredCallsWithParity(string framework)
    {
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        var environment=new Dictionary<string,string?> { ["FI_API_KEY"]="explicit-api-key",["FI_SECRET_KEY"]="explicit-secret-key",["FI_BASE_URL"]=server.Url };
        var baseline=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","round5_s6_catalog",new(environment) { ["DOTNET_STARTUP_HOOKS"]=null },framework);
        var traced=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","round5_s6_catalog",environment,framework);
        Assert.True(baseline.ExitCode==0,baseline.Error); Assert.True(traced.ExitCode==0,traced.Error);
        Assert.Equal("",baseline.Error); Assert.Equal("",traced.Error); Assert.Equal(baseline.Output,traced.Output);
        var bodies=server.RequestBodies.ToArray(); Assert.Equal(2,bodies.Length); Assert.Equal(bodies[0],bodies[1]);
        Assert.All(server.RequestHeaders,headers=>Assert.Equal("S6_HEADER_CREDENTIAL_9876",headers["x-vault-token"]));
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray(); Assert.Equal(2,spans.Length);
        Assert.DoesNotContain(spans,s=>s.Attributes.GetValueOrDefault("traceai.capture.layer") as string=="wire");
        foreach(var bytes in server.Exports)
        {
            string output=Encoding.UTF8.GetString(bytes);
            foreach(string secret in new[] { "S6_HEADER_CREDENTIAL_9876","S6_BODY_KEY_9876","S6_ARRAY_KEY_9876","S6_QUERY_CREDENTIAL_9876" }) Assert.DoesNotContain(secret,output);
        }
        Assert.Equal("<redacted> <redacted> <redacted> <redacted>",Assert.Single(spans,s=>s.Attributes.ContainsKey("later.echo")).Attributes["later.echo"]);
    }
}
