using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;
using OpenTelemetry;
using OpenTelemetry.Resources;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>Privacy catalog version 3 rules that apply to every exported string,
/// independent of attribute or field names.</summary>
public class CatalogVersionThreeTests : InitIsolation
{
    // Assembled at run time so that no credential-shaped literal sits in the source.
    private static readonly string ProviderKey="sk-"+"test-PLACEHOLDER-"+new string('0',10);
    private static readonly string AwsKey="AKIA"+new string('X',16);
    private static readonly string Jwt="eyJ"+"placeholder."+"placeholder."+"placeholder";
    private static readonly string Pem="-----BEGIN "+"PRIVATE KEY-----\nPLACEHOLDER\n-----END "+"PRIVATE KEY-----";
    private static readonly string TruncatedPem="-----BEGIN RSA "+"PRIVATE KEY-----\nPLACEHOLDER";
    public static IEnumerable<object[]> Shapes()=>new[] {
        new object[] { "note "+ProviderKey+" end","note <redacted> end" },
        new object[] { "note "+AwsKey+" end","note <redacted> end" },
        new object[] { "note "+Jwt+" end","note <redacted> end" },
        new object[] { "note Bearer PLACEHOLDER-VALUE end","note <redacted> end" },
        new object[] { "note basic\tUExBQ0VIT0xERVI= end","note <redacted> end" },
        new object[] { "note "+Pem+" end","note <redacted> end" },
        new object[] { "note "+TruncatedPem+" end","note <redacted>" }
    };
    [Theory]
    [MemberData(nameof(Shapes))]
    public void CredentialValueShapesAreRedactedInTextWithoutACredentialName(string text,string expected)
    {
        Assert.Equal(expected,WirePrivacy.ScrubString(text));
        Assert.Equal(expected,WirePrivacy.ScrubString(text,path:"output.value"));
        Assert.Equal(expected,WirePrivacy.ScrubString(expected));
    }
    [Theory]
    [InlineData("sk-short")] [InlineData("task-PLACEHOLDERPLACEHOLDER0000")] [InlineData("Bearer short")]
    [InlineData("Basic,authentication is described below")] [InlineData("ASIA is the largest continent")]
    [InlineData("eyJ alone is not a token")]
    public void NearMissesAreOrdinaryText(string text)
    {
        Assert.Equal(text,WirePrivacy.ScrubString(text));
        var publicKey="-----BEGIN PUBLIC KEY-----\nPLACEHOLDER\n-----END PUBLIC KEY-----";
        Assert.Equal(publicKey,WirePrivacy.ScrubString(publicKey));
    }
    [Fact]
    public void ValueShapesApplyInsideStructuredValuesAndSerializedJson()
    {
        var clean=JsonNode.Parse((string)WirePrivacy.Scrub(new JsonObject { ["notes"]="sent "+Jwt,["count"]=3 }))!;
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("{\"notes\":\"sent <redacted>\",\"count\":3}"),clean),clean.ToJsonString());
        string serialized=WireCore.J(new { messages=new[] { new { role="user",content="key "+ProviderKey } } });
        string scrubbed=WirePrivacy.ScrubString(serialized,path:"input.value");
        Assert.DoesNotContain(ProviderKey,scrubbed);
        Assert.Contains("key <redacted>",scrubbed);
    }
    [Fact]
    public void ExportBoundaryRedactsValueShapesInAttributesEventsAndStatusButNotTheOriginal()
    {
        using var original=new Activity("chat").Start();
        original.SetTag("output.value","answer "+ProviderKey);
        original.SetTag("custom.note",Jwt);
        original.AddEvent(new ActivityEvent("log",tags:new ActivityTagsCollection { ["message"]="sent "+AwsKey }));
        original.SetStatus(ActivityStatusCode.Error,"Bearer PLACEHOLDER-VALUE rejected");
        original.Stop();
        var privacy=new AutoProcessor(new AutoConfig(new AutoOptions { ApiKey="shape-api-key",SecretKey="shape-secret-key",ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp/absent.toml") }));
        var batch=new Batch<Activity>(new[] { original },1);
        var safe=Assert.Single(privacy.PrepareExport(in batch,Resource.Empty).Spans);
        Assert.Equal("answer <redacted>",safe.GetTagItem("output.value"));
        Assert.Equal("<redacted>",safe.GetTagItem("custom.note"));
        Assert.Equal("sent <redacted>",Assert.Single(safe.Events).Tags.Single().Value);
        Assert.Equal("<redacted> rejected",safe.StatusDescription);
        Assert.Equal("answer "+ProviderKey,original.GetTagItem("output.value"));
    }
    [Fact]
    public void WireSpansRedactValueShapesInMessagesAndRawBodies()
    {
        var request=new RequestData { Url="https://api.openai.com/v1/chat/completions",
            Body=Encoding.UTF8.GetBytes(WireCore.J(new { model="gpt-4o",messages=new[] { new { role="user",content="my key is "+ProviderKey } } })) };
        request.Headers["content-type"]="application/json";
        var response=new ResponseData { Status=200,Body="{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"ok\"}}]}"u8.ToArray() };
        response.Headers["content-type"]="application/json"; response.Size=response.Body.Length;
        var result=WireCore.Build(WireCore.Match("POST",request.Url)!,request,response);
        Assert.NotNull(result);
        Assert.Equal("my key is <redacted>",result.Attributes["gen_ai.input.messages.0.message.content"]);
        Assert.Equal("ok",result.Attributes["output.value"]);
        Assert.DoesNotContain(ProviderKey,WireCore.J(result.Attributes));
    }
    [Theory]
    [InlineData("gen_ai.response.time_to_first_token")] [InlineData("gen_ai.server.time_to_first_token")]
    [InlineData("gen_ai.server.time_per_output_token")] [InlineData("TimeToFirstToken")]
    [InlineData("output_tokens_per_second")] [InlineData("inter_token_latency")]
    public void TokenTimingMetricsAreNotCredentialsAndKeepNumericValues(string name)
    {
        Assert.False(WirePrivacy.CredentialKey(name));
        Assert.Equal(0.125,WirePrivacy.Scrub(0.125,path:name));
        Assert.Empty(WirePrivacy.HeaderSecrets(new Dictionary<string,string> { [name]="PLACEHOLDER-TIMING-VALUE" }));
    }
    [Theory]
    [InlineData("session_token")] [InlineData("api_token_latency_key")] [InlineData("token_secret")]
    [InlineData("secret.time_to_first_token")] [InlineData("time_to_first_token.api_key")]
    public void IndependentCredentialMarkersStillWinOverTokenTiming(string name)
    {
        Assert.True(WirePrivacy.CredentialKey(name));
        Assert.Equal("<redacted>",WirePrivacy.ScrubString("PLACEHOLDER-TIMING-VALUE",path:name));
    }
}
