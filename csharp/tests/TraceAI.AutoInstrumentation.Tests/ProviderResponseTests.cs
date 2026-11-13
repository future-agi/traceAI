using System.Text.Json.Nodes;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>The explicit provider-response root from the catalog's logprob_tokens list.</summary>
public class ProviderResponseTests
{
    private static readonly string ProviderKey="sk-"+"test-PLACEHOLDER-"+new string('0',10);
    private static JsonNode Chat(string content,string token,string alternative="world")=>JsonNode.Parse(new JsonObject {
        ["id"]="chatcmpl-local",
        ["choices"]=new JsonArray(new JsonObject {
            ["index"]=0,
            ["message"]=new JsonObject { ["role"]="assistant",["content"]=content },
            ["logprobs"]=new JsonObject { ["content"]=new JsonArray(new JsonObject {
                ["token"]=token,["logprob"]=-0.25,["bytes"]=new JsonArray(72,105),
                ["top_logprobs"]=new JsonArray(new JsonObject { ["token"]=alternative,["logprob"]=-1.5,["bytes"]=new JsonArray(119) })
            }) }
        })
    }.ToJsonString())!;
    private static JsonNode? Token(JsonNode? root,params object[] path)
    {
        foreach(var step in path) root=step is int i?root?[i]:root?[(string)step];
        return root;
    }
    private static string? Text(JsonNode? node)=>WireCore.S(node);
    [Fact]
    public void OrdinaryLogprobTokensSurviveOnlyAtTheExplicitProviderRoot()
    {
        var response=Chat("Hello world","Hello");
        var clean=ProviderResponse.Scrub(response);
        Assert.Equal("Hello",Text(Token(clean,"choices",0,"logprobs","content",0,"token")));
        Assert.Equal("world",Text(Token(clean,"choices",0,"logprobs","content",0,"top_logprobs",0,"token")));
        Assert.Equal("[72,105]",Token(clean,"choices",0,"logprobs","content",0,"bytes")!.ToJsonString());
        Assert.Equal("Hello world",Text(Token(clean,"choices",0,"message","content")));
        // The same structure anywhere else keeps the generic name rule.
        var generic=PrivacyCatalog.Scrub(response);
        Assert.Equal("<redacted>",Text(Token(generic,"choices",0,"logprobs","content",0,"token")));
        Assert.DoesNotContain("\"token\":\"Hello\"",WirePrivacy.ScrubString(response.ToJsonString(),path:"output.value"));
        // The input is never mutated.
        Assert.Equal("Hello",Text(Token(response,"choices",0,"logprobs","content",0,"token")));
    }
    [Fact]
    public void SecretOrCredentialShapedTokensAreMaskedTogetherWithTheirBytes()
    {
        var response=Chat("fine","Bearer PLACEHOLDER-VALUE"," PLACEHOLDER-SECRET-0001");
        var clean=ProviderResponse.Scrub(response,["PLACEHOLDER-SECRET-0001"]);
        Assert.Equal("<redacted>",Text(Token(clean,"choices",0,"logprobs","content",0,"token")));
        Assert.Equal("<redacted>",Text(Token(clean,"choices",0,"logprobs","content",0,"bytes")));
        Assert.Equal(" <redacted>",Text(Token(clean,"choices",0,"logprobs","content",0,"top_logprobs",0,"token")));
        Assert.Equal("<redacted>",Text(Token(clean,"choices",0,"logprobs","content",0,"top_logprobs",0,"bytes")));
        Assert.Equal("fine",Text(Token(clean,"choices",0,"message","content")));
    }
    [Fact]
    public void MaskedChoiceTextMasksTheWholeLogprobsBlock()
    {
        var clean=ProviderResponse.Scrub(Chat("my key is "+ProviderKey,"my"));
        Assert.Equal("my key is <redacted>",Text(Token(clean,"choices",0,"message","content")));
        Assert.Equal("<redacted>",Text(Token(clean,"choices",0,"logprobs")));
        Assert.DoesNotContain(ProviderKey,clean!.ToJsonString());
    }
    [Fact]
    public void StreamedDeltasAreJoinedByChoiceIndexBeforeAnyTokenIsKept()
    {
        static JsonObject Chunk(int index,string delta,string token)=>new() {
            ["choices"]=new JsonArray(new JsonObject {
                ["index"]=index,["delta"]=new JsonObject { ["content"]=delta },
                ["logprobs"]=new JsonObject { ["content"]=new JsonArray(new JsonObject { ["token"]=token,["logprob"]=-0.1 }) }
            })
        };
        // Neither fragment of choice 0 is a credential on its own; joined they are.
        var stream=JsonNode.Parse(new JsonArray(Chunk(0,"Bearer PLACE","Bearer"),Chunk(1,"plain","plain"),Chunk(0,"HOLDER-VALUE","HOLDER")).ToJsonString())!;
        var clean=(JsonArray)ProviderResponse.Scrub(stream)!;
        foreach(int chunk in new[] { 0,2 })
        {
            Assert.Equal("<redacted>",Text(Token(clean,chunk,"choices",0,"logprobs")));
            Assert.Equal("<redacted>",Text(Token(clean,chunk,"choices",0,"delta","content")));
        }
        Assert.Equal("plain",Text(Token(clean,1,"choices",0,"delta","content")));
        Assert.Equal("plain",Text(Token(clean,1,"choices",0,"logprobs","content",0,"token")));
        Assert.DoesNotContain("PLACE",clean.ToJsonString());
    }
    [Fact]
    public void StreamedChoicesWithoutAnIndexCannotBeJoinedAndAreMasked()
    {
        var stream=JsonNode.Parse("[{\"choices\":[{\"delta\":{\"content\":\"hi\"},\"logprobs\":{\"content\":[{\"token\":\"hi\"}]}}]}]")!;
        Assert.Equal("<redacted>",Text(Token(ProviderResponse.Scrub(stream),0,"choices",0,"logprobs")));
    }
    [Fact]
    public void WildcardsNeedRealArraysAndIndependentCredentialsStayPrivate()
    {
        var notArray=JsonNode.Parse("{\"choices\":{\"0\":{\"logprobs\":{\"content\":[{\"token\":\"Hello\"}]}}}}")!;
        Assert.Equal("<redacted>",Text(Token(ProviderResponse.Scrub(notArray),"choices","0","logprobs","content",0,"token")));
        var response=Chat("Hello","Hello");
        Token(response,"choices",0,"logprobs","content",0)!.AsObject()["api_key"]="PLACEHOLDER-SECRET-0002";
        var clean=ProviderResponse.Scrub(response);
        Assert.Equal("Hello",Text(Token(clean,"choices",0,"logprobs","content",0,"token")));
        Assert.Equal("<redacted>",Text(Token(clean,"choices",0,"logprobs","content",0,"api_key")));
    }
    [Fact]
    public void MediaHintsOnTheWayKeepTheGenericRedaction()
    {
        var response=Chat("Hello","Hello");
        Token(response,"choices",0,"logprobs")!.AsObject()["mime_type"]="audio/wav";
        Assert.Equal("<redacted>",Text(Token(ProviderResponse.Scrub(response),"choices",0,"logprobs","content",0,"token")));
    }
    [Fact]
    public void UnknownFormatsUseTheGenericRules()
    {
        var clean=ProviderResponse.Scrub(Chat("Hello","Hello"),format:"anthropic.messages");
        Assert.Equal("<redacted>",Text(Token(clean,"choices",0,"logprobs","content",0,"token")));
    }
}
