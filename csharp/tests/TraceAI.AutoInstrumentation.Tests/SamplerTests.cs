using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using OpenTelemetry;
using OpenTelemetry.Trace;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class SamplerTests : InitIsolation
{
    private sealed class EnvironmentScope : IDisposable
    {
        private readonly Dictionary<string,string?> _previous=new();
        internal EnvironmentScope(string? sampler=null,string? arg=null,string? rate=null)
        {
            Set("OTEL_TRACES_SAMPLER",sampler); Set("OTEL_TRACES_SAMPLER_ARG",arg); Set("FI_SAMPLE_RATE",rate);
            var warnings=(ConcurrentDictionary<string,byte>)typeof(TraceAIAuto)
                .GetField("Warnings",BindingFlags.NonPublic|BindingFlags.Static)!.GetValue(null)!;
            warnings.TryRemove("sampler",out _);
        }
        private void Set(string name,string? value)
        { _previous[name]=Environment.GetEnvironmentVariable(name); Environment.SetEnvironmentVariable(name,value); }
        public void Dispose()
        { foreach(var pair in _previous) Environment.SetEnvironmentVariable(pair.Key,pair.Value); }
    }

    private static AutoConfig Config(MemoryExporter exporter,double? rate=null,TracerProvider? provider=null)=>new(new AutoOptions {
        Exporter=exporter,Provider=provider,SampleRate=rate,WireCapture=false,
        ConfigFile=Path.Combine(Path.GetTempPath(),"traceai-sampler-"+Guid.NewGuid()+".toml") });

    private static ActivityContext Parent(bool sampled,bool remote)=>new(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),
        sampled?ActivityTraceFlags.Recorded:ActivityTraceFlags.None,isRemote:remote);

    [Fact]
    public void EnvironmentAlwaysOffDropsEvenWhenAnotherProviderRecords()
    {
        using var environment=new EnvironmentScope("always_off");
        var hostExporter=new MemoryExporter(); var exporter=new MemoryExporter();
        using var host=Sdk.CreateTracerProviderBuilder().AddSource("OpenAI.Sampler.Shared")
            .SetSampler(new AlwaysOnSampler()).AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        using var agent=new Agent(Config(exporter));
        using var source=new ActivitySource("OpenAI.Sampler.Shared");
        using(var activity=source.StartActivity("chat")) { Assert.NotNull(activity); Assert.True(activity.Recorded); }
        Assert.True(agent.Flush(3000)); Assert.Single(hostExporter.Spans); Assert.Empty(exporter.Spans);
    }

    [Theory]
    [InlineData("always_off")]
    [InlineData("traceidratio")]
    [InlineData("parentbased_traceidratio")]
    [InlineData("parentbased_always_off")]
    public void EnvironmentZeroSamplersDropRoots(string name)
    {
        using var environment=new EnvironmentScope(name,"0");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using(var activity=TraceAIAuto.Source.StartActivity("root")) Assert.False(activity?.Recorded??false);
        Assert.True(agent.Flush(3000)); Assert.Empty(exporter.Spans);
    }

    [Theory]
    [InlineData("parentbased_always_on",true)]
    [InlineData("parentbased_always_on",false)]
    [InlineData("parentbased_always_off",true)]
    [InlineData("parentbased_always_off",false)]
    [InlineData("parentbased_traceidratio",true)]
    [InlineData("parentbased_traceidratio",false)]
    public void ParentBasedEnvironmentHonoursSampledAndUnsampledParents(string name,bool remote)
    {
        using var environment=new EnvironmentScope(name,"0");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using(TraceAIAuto.Source.StartActivity("sampled-parent",ActivityKind.Internal,Parent(true,remote))) { }
        using(TraceAIAuto.Source.StartActivity("unsampled-parent",ActivityKind.Internal,Parent(false,remote))) { }
        Assert.True(agent.Flush(3000));
        var span=Assert.Single(exporter.Spans); Assert.Equal("sampled-parent",span.DisplayName); Assert.True(span.Recorded);
    }

    [Theory]
    [InlineData("always_on",true)]
    [InlineData("always_on",false)]
    [InlineData("traceidratio",true)]
    [InlineData("traceidratio",false)]
    public void NonParentBasedSamplersIgnoreUnsampledParents(string name,bool remote)
    {
        using var environment=new EnvironmentScope(name,"1");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using(TraceAIAuto.Source.StartActivity("child",ActivityKind.Internal,Parent(false,remote))) { }
        Assert.True(agent.Flush(3000)); Assert.True(Assert.Single(exporter.Spans).Recorded);
    }

    [Fact]
    public void ParentBasedEnvironmentKeepsOwnLocalChildrenWithoutChangingHostFlags()
    {
        using var environment=new EnvironmentScope("parentbased_always_on");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using(var parent=TraceAIAuto.Source.StartActivity("parent"))
        {
            Assert.NotNull(parent); Assert.False(parent.Recorded);
            using(var child=TraceAIAuto.Source.StartActivity("child"))
            { Assert.NotNull(child); Assert.False(child.Recorded); Assert.Equal(parent.SpanId,child.ParentSpanId); }
        }
        Assert.True(agent.Flush(3000)); Assert.Equal(2,exporter.Spans.Count); Assert.All(exporter.Spans,span=>Assert.True(span.Recorded));
    }

    [Theory]
    [InlineData(true,0)] [InlineData(true,1)]
    [InlineData(false,0)] [InlineData(false,1)]
    public void ExplicitRateWinsOverEnvironment(bool option,int rate)
    {
        using var environment=new EnvironmentScope(rate==0?"always_on":"always_off",rate:option?null:rate.ToString());
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter,option?rate:null));
        using(TraceAIAuto.Source.StartActivity("root")) { }
        Assert.True(agent.Flush(3000)); Assert.Equal(rate,exporter.Spans.Count);
    }

    [Fact]
    public void ExplicitRateStillUsesParentBasedSamplingForLocalParents()
    {
        using var environment=new EnvironmentScope("always_on");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter,0));
        using(TraceAIAuto.Source.StartActivity("child",ActivityKind.Internal,Parent(true,false))) { }
        Assert.True(agent.Flush(3000)); Assert.True(Assert.Single(exporter.Spans).Recorded);
    }

    [Theory]
    [InlineData("not_a_sampler",null)]
    [InlineData(" always_off ",null)]
    [InlineData("traceidratio","invalid")]
    [InlineData("traceidratio","NaN")]
    [InlineData("traceidratio","Infinity")]
    [InlineData("traceidratio","-0.1")]
    [InlineData("parentbased_traceidratio","1.1")]
    public void InvalidEnvironmentFallsBackToParentBasedOneWithOneWarning(string name,string? arg)
    {
        using var environment=new EnvironmentScope(name,arg);
        using var errors=new StringWriter(); var previous=Console.Error;
        try
        {
            Console.SetError(errors);
            var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
            using(TraceAIAuto.Source.StartActivity("root")) { }
            using(TraceAIAuto.Source.StartActivity("unsampled-parent",ActivityKind.Internal,Parent(false,false))) { }
            Assert.True(agent.Flush(3000)); Assert.Equal("root",Assert.Single(exporter.Spans).DisplayName);
            foreach(var remote in new[] { true,false })
            {
                using(TraceAIAuto.Source.StartActivity("sampled-parent",ActivityKind.Internal,Parent(true,remote))) { }
                using(TraceAIAuto.Source.StartActivity("unsampled-parent",ActivityKind.Internal,Parent(false,remote))) { }
            }
            Assert.True(agent.Flush(3000)); Assert.Equal(3,exporter.Spans.Count);
            Assert.DoesNotContain(exporter.Spans,span=>span.DisplayName=="unsampled-parent");
            Assert.All(exporter.Spans,span=>Assert.True(span.Recorded));
            Assert.Single(errors.ToString().Split('\n',StringSplitOptions.RemoveEmptyEntries),line=>line.Contains("OTEL_TRACES_SAMPLER"));
            Assert.DoesNotContain(arg??name,errors.ToString());
        }
        finally { Console.SetError(previous); }
    }

    [Theory]
    [InlineData(true)] [InlineData(false)]
    public void SuppliedProviderKeepsItsSamplerDespiteFiAndOtelOptions(bool sampled)
    {
        using var environment=new EnvironmentScope(sampled?"always_off":"always_on",rate:sampled?"0":"1");
        var hostExporter=new MemoryExporter(); var exporter=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .SetSampler(sampled?new AlwaysOnSampler():new AlwaysOffSampler())
            .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        using(var agent=new Agent(Config(exporter,provider:provider)))
        {
            using(var activity=TraceAIAuto.Source.StartActivity("host")) Assert.Equal(sampled,activity?.Recorded??false);
            Assert.True(agent.Flush(3000)); Assert.Equal(sampled?1:0,hostExporter.Spans.Count); Assert.Equal(sampled?1:0,exporter.Spans.Count);
        }
        using(TraceAIAuto.Source.StartActivity("after-agent")) { }
        Assert.Equal(sampled?2:0,hostExporter.Spans.Count);
    }

    [Fact]
    public void SnapshotDoesNotInventASampledFlagForRecordOnlyActivities()
    {
        using var environment=new EnvironmentScope();
        var privacy=new AutoProcessor(Config(new MemoryExporter()));
        using var activity=new Activity("record-only").SetIdFormat(ActivityIdFormat.W3C).Start();
        activity.ActivityTraceFlags=ActivityTraceFlags.None; activity.Stop();
        var batch=new Batch<Activity>(new[] { activity },1);
        var snapshot=privacy.PrepareExport(in batch,OpenTelemetry.Resources.Resource.Empty);
        Assert.False(Assert.Single(snapshot.Spans).Recorded); Assert.False(activity.Recorded);
    }
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void AcceptedRecordOnlySpanStaysSampledThroughThePrivacyFallback(bool fallback)
    {
        using var activity=new Activity("chat model").SetIdFormat(ActivityIdFormat.W3C).Start();
        activity.SetTag("gen_ai.operation.name","chat"); activity.SetTag("output.value","private");
        activity.ActivityTraceFlags=ActivityTraceFlags.None; activity.Stop();
        // A sanitizer failure sends the span down the privacy fallback, which rebuilds the copy.
        object? Safe(string key,object value,string? operation)=>fallback && key=="output.value"?throw new InvalidDataException():value;
        var accepted=ExportSnapshot.Create([activity],OpenTelemetry.Resources.Resource.Empty,Safe,text=>text,sampled:_=>true);
        var span=Assert.Single(accepted.Spans);
        Assert.True(span.Recorded); Assert.False(activity.Recorded);
        if(fallback) Assert.Null(span.GetTagItem("output.value"));
        var rejected=ExportSnapshot.Create([activity],OpenTelemetry.Resources.Resource.Empty,Safe,text=>text,sampled:_=>false);
        Assert.Empty(rejected.Spans);
    }
}
