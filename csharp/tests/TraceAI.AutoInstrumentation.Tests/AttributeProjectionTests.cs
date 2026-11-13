using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Resources;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class AttributeProjectionTests : InitIsolation
{
    private static Activity Snapshot(IEnumerable<KeyValuePair<string,object?>> tags,int limit)
    {
        string? previous=Environment.GetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT");
        Environment.SetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT",limit.ToString());
        try
        {
            using var original=new Activity("chat").Start();
            foreach(var tag in tags) original.SetTag(tag.Key,tag.Value);
            original.Stop();
            var privacy=new AutoProcessor(new AutoConfig(new AutoOptions { ApiKey="projection-key",SecretKey="projection-secret",ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp/absent.toml") }));
            var batch=new Batch<Activity>(new[] { original },1);
            return Assert.Single(privacy.PrepareExport(in batch,Resource.Empty).Spans);
        }
        finally { Environment.SetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT",previous); }
    }
    private static Dictionary<string,object?> Conversation()=>new() {
        ["gen_ai.span.kind"]="LLM",["traceai.capture.layer"]="wire",["gen_ai.operation.name"]="chat",
        ["gen_ai.output.messages.0.message.role"]="assistant",["gen_ai.output.messages.0.message.content"]="answer",
        ["output.value"]="answer",["output.mime_type"]="text/plain",
        ["gen_ai.input.messages.0.message.role"]="user",["gen_ai.input.messages.0.message.content"]="oldest",
        ["gen_ai.input.messages.2.message.role"]="user",["gen_ai.input.messages.2.message.content"]="middle",
        ["gen_ai.input.messages.10.message.role"]="user",["gen_ai.input.messages.10.message.content"]="newest",
        ["input.value"]="duplicate conversation",["input.mime_type"]="text/plain",
        ["gen_ai.request.parameters"]="{}"
    };
    [Fact]
    public void OutputWinsAndNewestInputSuffixIsAtomicWithTruthfulAccumulatedCounts()
    {
        var tags=Conversation(); tags["traceai.capture.dropped_content_count"]=7L;
        using var snapshot=Snapshot(tags,12);
        Assert.Equal(12,snapshot.TagObjects.Count());
        Assert.Equal("answer",snapshot.GetTagItem("gen_ai.output.messages.0.message.content"));
        Assert.Equal("newest",snapshot.GetTagItem("gen_ai.input.messages.10.message.content"));
        Assert.Equal("middle",snapshot.GetTagItem("gen_ai.input.messages.2.message.content"));
        Assert.Null(snapshot.GetTagItem("gen_ai.input.messages.0.message.role"));
        Assert.Null(snapshot.GetTagItem("input.value"));
        Assert.Equal(12L,snapshot.GetTagItem("traceai.capture.dropped_content_count"));
        using var reversed=Snapshot(tags.Reverse(),12);
        Assert.Equal(snapshot.TagObjects,reversed.TagObjects);
        using var repeated=Snapshot(snapshot.TagObjects,12);
        Assert.Equal(snapshot.TagObjects,repeated.TagObjects);
    }
    [Fact]
    public void UnfittableNewerInputBlocksOlderInputAndAggregateDuplication()
    {
        var tags=Conversation();
        tags["gen_ai.input.messages.10.message.tool_call_id"]="call";
        using var snapshot=Snapshot(tags,10);
        Assert.DoesNotContain(snapshot.TagObjects,pair=>pair.Key.StartsWith("gen_ai.input.messages",StringComparison.Ordinal));
        Assert.Null(snapshot.GetTagItem("input.value"));
        Assert.Equal(9L,snapshot.GetTagItem("traceai.capture.dropped_content_count"));
    }
    [Fact]
    public void UnfittableOutputPreventsLowerPriorityInput()
    {
        var tags=Conversation();
        tags["gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"]="tool";
        using var snapshot=Snapshot(tags,6);
        Assert.DoesNotContain(snapshot.TagObjects,pair=>pair.Key.StartsWith("gen_ai.input.messages",StringComparison.Ordinal));
        Assert.Null(snapshot.GetTagItem("input.value"));
        Assert.Null(snapshot.GetTagItem("gen_ai.output.messages.0.message.role"));
    }
    [Fact]
    public void ProtectedOverflowFollowsContractFamilyAndLexicalOrder()
    {
        var tags=Conversation();
        tags["traceai.wire.truncated"]=true; tags["traceai.wire.incomplete"]=true;
        tags["gen_ai.response.id"]="response"; tags["project_name"]="project";
        tags["error.type"]="TimeoutException"; tags["gen_ai.usage.total_tokens"]=3L;
        using var snapshot=Snapshot(tags,6);
        Assert.Equal(new[] { "traceai.capture.dropped_content_count","traceai.wire.incomplete","traceai.wire.truncated","gen_ai.response.id","gen_ai.span.kind","project_name" },snapshot.TagObjects.Select(p=>p.Key));
        Assert.Equal(13L,snapshot.GetTagItem("traceai.capture.dropped_content_count"));
    }
    [Theory]
    [InlineData(4)] [InlineData(5)]
    public void ProjectionNeverSeparatesMimeFromPayloadAndScrubsBeforeSelecting(int limit)
    {
        var tags=new Dictionary<string,object?> { ["gen_ai.span.kind"]="LLM",["traceai.capture.layer"]="wire",
            ["output.parts.0.content"]="AQIDBA==",["output.parts.0.mime_type"]="audio/pcm",
            ["output.value"]="answer",["output.mime_type"]="text/plain" };
        using var snapshot=Snapshot(tags,limit);
        Assert.True(snapshot.TagObjects.Count()<=limit);
        Assert.Equal(snapshot.GetTagItem("output.parts.0.content")!=null,snapshot.GetTagItem("output.parts.0.mime_type")!=null);
        Assert.Equal(snapshot.GetTagItem("output.value")!=null,snapshot.GetTagItem("output.mime_type")!=null);
        Assert.DoesNotContain("AQIDBA==",WireCore.J(snapshot.TagObjects));
    }
    [Theory]
    [InlineData(0)] [InlineData(8)] [InlineData(128)]
    public void WireCandidatesAreProjectedBeforeActivityListenersSeeThem(int limit)
    {
        KeyValuePair<string,object?>[]? observed=null;
        using var listener=new ActivityListener {
            ShouldListenTo=source=>source.Name==TraceAIAuto.Source.Name,
            Sample=(ref ActivityCreationOptions<ActivityContext> _)=>ActivitySamplingResult.AllDataAndRecorded,
            ActivityStarted=activity=>observed=activity.TagObjects.ToArray()
        };
        ActivitySource.AddActivityListener(listener);
        var request=new RequestData { Url="https://example.invalid/v1/chat/completions",Body=System.Text.Encoding.UTF8.GetBytes(WireCore.J(new {
            model="m",messages=Enumerable.Range(0,70).Select(i=>new { role="user",content="message "+i }).ToArray()
        })) };
        var capture=new Capture(WireCore.Match("POST",request.Url)!,request,4194304,default,DateTimeOffset.UtcNow,[],attributeLimit:limit);
        capture.Response.Status=200; capture.Response.Headers["content-type"]="application/json";
        capture.Bytes("{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"answer\"}}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":4}}"u8);
        capture.Complete();
        Assert.NotNull(observed); Assert.True(observed.Length<=limit);
        if(limit>0) Assert.Contains(observed,pair=>pair.Key==AttributeProjection.DroppedContent);
        if(limit==128)
        {
            Assert.Contains(observed,pair=>pair.Key=="gen_ai.input.messages.69.message.content");
            Assert.Contains(observed,pair=>pair.Key=="output.value"&&Equals(pair.Value,"answer"));
            Assert.DoesNotContain(observed,pair=>pair.Key=="gen_ai.input.messages.0.message.content"||pair.Key=="input.value");
        }
    }
    [Fact]
    public void MimeGroupingDoesNotCountUnrelatedAudioMetadataAsDroppedContent()
    {
        var tags=new Dictionary<string,object?> { ["gen_ai.span.kind"]="LLM",["traceai.capture.layer"]="wire",
            ["parts.0.content"]="AQIDBA==",["parts.0.mime_type"]="audio/pcm",["parts.0.voice"]="alloy" };
        using var snapshot=Snapshot(tags,3);
        Assert.Equal(2L,snapshot.GetTagItem("traceai.capture.dropped_content_count"));
    }
    [Fact]
    public void ResolvedExporterLimitOutlivesEnvironmentChanges()
    {
        string? previous=Environment.GetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT");
        try
        {
            Environment.SetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT","8");
            using var exporter=new OpenTelemetry.Exporter.OtlpTraceExporter(new() { Endpoint=new Uri("http://127.0.0.1:1/traces") });
            Environment.SetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT","128");
            Assert.Equal(8,AttributeProjection.Limit(exporter));
        }
        finally { Environment.SetEnvironmentVariable("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT",previous); }
    }
    [Theory]
    [InlineData(0)] [InlineData(128)]
    public void ProjectionRespectsZeroAndDefaultLimits(int limit)
    {
        var tags=Conversation();
        for(int i=11;i<100;i++) { tags[$"gen_ai.input.messages.{i}.message.role"]="user"; tags[$"gen_ai.input.messages.{i}.message.content"]=i.ToString(); }
        using var snapshot=Snapshot(tags,limit);
        Assert.True(snapshot.TagObjects.Count()<=limit);
        if(limit==128) { Assert.NotNull(snapshot.GetTagItem("traceai.capture.dropped_content_count")); Assert.NotNull(snapshot.GetTagItem("gen_ai.input.messages.99.message.content")); Assert.Null(snapshot.GetTagItem("gen_ai.input.messages.0.message.content")); }
    }
}
