using System.Text;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class TokenAlgebraTests
{
    private static SpanResult Build(string path,string body,string contentType="application/json")
    {
        // Parsed only; these tests never create an HTTP client or send a request.
        string url=(path.StartsWith("/model/",StringComparison.Ordinal)?"https://bedrock-runtime.us-east-1.amazonaws.com":"https://example.invalid")+path;
        var request=new RequestData { Url=url,Body="{\"model\":\"m\",\"messages\":[],\"contents\":[],\"input\":\"hello\"}"u8.ToArray() };
        return WireCore.Build(WireCore.Match("POST",url)!,request,new ResponseData { Status=200,Body=Encoding.UTF8.GetBytes(body),Headers=new() { ["content-type"]=contentType } })!;
    }
    [Theory]
    [InlineData("/v1/messages","{\"content\":[],\"usage\":{\"input_tokens\":10,\"output_tokens\":4,\"cache_read_input_tokens\":20,\"cache_creation_input_tokens\":30,\"cache_creation\":{\"ephemeral_5m_input_tokens\":15,\"ephemeral_1h_input_tokens\":15}}}",60,4,64)]
    [InlineData("/model/m/invoke","{\"content\":[],\"usage\":{\"input_tokens\":10,\"output_tokens\":4,\"cache_read_input_tokens\":20,\"cache_creation_input_tokens\":30}}",60,4,64)]
    [InlineData("/model/m/converse","{\"usage\":{\"inputTokens\":10,\"outputTokens\":4,\"cacheReadInputTokens\":20,\"cacheWriteInputTokens\":30,\"totalTokens\":14}}",60,4,64)]
    [InlineData("/v1beta/models/m:generateContent","{\"usageMetadata\":{\"promptTokenCount\":10,\"toolUsePromptTokenCount\":2,\"candidatesTokenCount\":4,\"thoughtsTokenCount\":3,\"cachedContentTokenCount\":5,\"totalTokenCount\":14}}",12,7,19)]
    [InlineData("/v1/chat/completions","{\"choices\":[],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":4,\"prompt_tokens_details\":{\"cached_tokens\":5},\"completion_tokens_details\":{\"reasoning_tokens\":3},\"total_tokens\":999}}",10,4,14)]
    [InlineData("/v1/messages","{\"content\":[],\"usage\":{\"input_tokens\":0,\"output_tokens\":0}}",0,0,0)]
    public void NormalizeInclusiveUsageExactlyOnce(string path,string body,long input,long output,long total)
    {
        var attributes=Build(path,body).Attributes;
        Assert.Equal(input,attributes["gen_ai.usage.input_tokens"]); Assert.Equal(output,attributes["gen_ai.usage.output_tokens"]); Assert.Equal(total,attributes["gen_ai.usage.total_tokens"]);
        if(body.Contains("totalTokens\":14",StringComparison.Ordinal)||body.Contains("totalTokenCount\":14",StringComparison.Ordinal)) Assert.Equal(14L,attributes["gen_ai.usage.provider_total_tokens"]);
        if(body.Contains("999",StringComparison.Ordinal)) Assert.Equal(999L,attributes["gen_ai.usage.provider_total_tokens"]);
    }
    [Theory]
    [InlineData("{\"input_tokens\":null,\"output_tokens\":4,\"cache_read_input_tokens\":20}",false)]
    [InlineData("{\"input_tokens\":10,\"output_tokens\":4,\"cache_read_input_tokens\":null}",false)]
    [InlineData("{\"output_tokens\":4,\"cache_read_input_tokens\":20}",false)]
    [InlineData("{\"input_tokens\":10,\"output_tokens\":4}",true)]
    public void UnknownAndExplicitNullCountersAreNeverManufacturedAsZero(string usage,bool inputKnown)
    {
        var attributes=Build("/v1/messages","{\"content\":[],\"usage\":"+usage+"}").Attributes;
        Assert.Equal(inputKnown,attributes.ContainsKey("gen_ai.usage.input_tokens"));
        Assert.Equal(inputKnown,attributes.ContainsKey("gen_ai.usage.total_tokens"));
        Assert.Equal(4L,attributes["gen_ai.usage.output_tokens"]);
    }
    [Fact]
    public void StreamingCumulativeAnthropicSnapshotsMergeBeforeNormalization()
    {
        const string stream="data: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":10,\"output_tokens\":0,\"cache_read_input_tokens\":20,\"cache_creation_input_tokens\":30}}}\n\n"+
            "data: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":2}}\n\n"+
            "data: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":4,\"cache_read_input_tokens\":20}}\n\n";
        var attributes=Build("/v1/messages",stream,"text/event-stream").Attributes;
        Assert.Equal(60L,attributes["gen_ai.usage.input_tokens"]); Assert.Equal(4L,attributes["gen_ai.usage.output_tokens"]); Assert.Equal(64L,attributes["gen_ai.usage.total_tokens"]);
        Assert.Equal(20L,attributes["gen_ai.usage.cache_read_tokens"]); Assert.Equal(30L,attributes["gen_ai.usage.cache_write_tokens"]);
    }
    [Fact]
    public void LaterNullChatBaseCounterClearsEarlierCumulativeValue()
    {
        const string stream="data: {\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":4}}\n\n"+
            "data: {\"usage\":{\"prompt_tokens\":null,\"completion_tokens\":0}}\n\n";
        var attributes=Build("/v1/chat/completions",stream,"text/event-stream").Attributes;
        Assert.False(attributes.ContainsKey("gen_ai.usage.input_tokens"));
        Assert.False(attributes.ContainsKey("gen_ai.usage.total_tokens"));
        Assert.Equal(0L,attributes["gen_ai.usage.output_tokens"]);
    }
    [Theory]
    [InlineData("prompt_tokens_details","cached_tokens","cache_read_tokens")]
    [InlineData("input_tokens_details","audio_tokens","input_tokens.audio")]
    [InlineData("completion_tokens_details","reasoning_tokens","output_tokens.reasoning")]
    [InlineData("output_tokens_details","audio_tokens","output_tokens.audio")]
    public void NullDetailObjectReplacesEarlierSnapshotRatherThanRetainingItsFields(string details,string field,string attribute)
    {
        string stream="data: {\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":4,\""+details+"\":{\""+field+"\":3}}}\n\n"+
            "data: {\"usage\":{\""+details+"\":null}}\n\n";
        var attributes=Build("/v1/chat/completions",stream,"text/event-stream").Attributes;
        Assert.False(attributes.ContainsKey("gen_ai.usage."+attribute));
        Assert.Equal(14L,attributes["gen_ai.usage.total_tokens"]);
    }
    [Theory]
    [InlineData("-1")]
    [InlineData("1.5")]
    public void InvalidTokenCountersStayUnknown(string value)
    {
        var attributes=Build("/v1/messages","{\"content\":[],\"usage\":{\"input_tokens\":"+value+",\"output_tokens\":4}}").Attributes;
        Assert.False(attributes.ContainsKey("gen_ai.usage.input_tokens"));
        Assert.False(attributes.ContainsKey("gen_ai.usage.total_tokens"));
    }
    [Fact]
    public void InputOnlyUsageRetainsProviderTotalWithoutInventingOutput()
    {
        var attributes=Build("/v1/embeddings","{\"data\":[],\"usage\":{\"prompt_tokens\":10,\"total_tokens\":10}}").Attributes;
        Assert.Equal(10L,attributes["gen_ai.usage.input_tokens"]); Assert.Equal(10L,attributes["gen_ai.usage.total_tokens"]); Assert.False(attributes.ContainsKey("gen_ai.usage.output_tokens"));
    }
}
