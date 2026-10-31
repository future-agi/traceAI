using System.Text;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class StreamingContractTests
{
    [Theory]
    [InlineData("text/event-stream","data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3}}\n")]
    [InlineData("application/x-ndjson","{\"choices\":[{\"delta\":{\"content\":\"answer\"}}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3}}")]
    public void UndelimitedFramesNeverEstablishOutputOrUsageEvenAtEof(string type,string body)
    {
        var response=new ResponseData { Status=200,Body=Encoding.UTF8.GetBytes(body),Headers=new() { ["content-type"]=type } };
        response.ObserveChunk(response.Body.Length,0.01);
        var request=new RequestData { Url="https://example.invalid/v1/chat/completions",Body="{\"model\":\"m\",\"messages\":[]}"u8.ToArray() };
        var result=WireCore.Build(WireCore.Match("POST",request.Url)!,request,response)!;
        Assert.False(result.Attributes.ContainsKey("gen_ai.response.time_to_first_text"));
        Assert.False(result.Attributes.ContainsKey("gen_ai.usage.input_tokens"));
        Assert.False(result.Attributes.ContainsKey("output.value"));
    }
    [Fact]
    public void StreamingSingleJsonObjectUsesItsClosingByteArrival()
    {
        byte[] body="{\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"answer\"}]}}]}  "u8.ToArray();
        var response=new ResponseData { Status=200,Body=body,Headers=new() { ["content-type"]="application/json" } };
        response.ObserveChunk(1,0.01); response.ObserveChunk(body.Length-3,0.03); response.ObserveChunk(2,0.07);
        var request=new RequestData { Url="https://example.invalid/v1beta/models/m:streamGenerateContent",Body="{\"contents\":[]}"u8.ToArray() };
        var result=WireCore.Build(WireCore.Match("POST",request.Url)!,request,response)!;
        Assert.Equal(0.01,result.Attributes["gen_ai.response.time_to_first_chunk"]);
        Assert.Equal(0.03,result.Attributes["gen_ai.response.time_to_first_text"]);
    }
    [Fact]
    public void LostArrivalAssociationMarksIncompleteWithoutGuessing()
    {
        var heartbeat="data: {}\n\n"u8.ToArray();
        var output="data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n"u8.ToArray();
        var response=new ResponseData { Status=200,Headers=new() { ["content-type"]="text/event-stream" } };
        using var bytes=new MemoryStream();
        for(int i=0;i<StreamMilestones.MaxArrivals;i++) { bytes.Write(heartbeat); response.ObserveChunk(heartbeat.Length,i/1000.0); }
        bytes.Write(output); response.ObserveChunk(output.Length,10); response.Body=bytes.ToArray();
        var request=new RequestData { Url="https://example.invalid/v1/chat/completions",Body="{\"messages\":[]}"u8.ToArray() };
        var result=WireCore.Build(WireCore.Match("POST",request.Url)!,request,response)!;
        Assert.Equal(true,result.Attributes["traceai.wire.incomplete"]);
        Assert.False(result.Attributes.ContainsKey("gen_ai.response.time_to_first_text"));
    }
}
