using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
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

    private static ExportSampler FractionalSampler()
    {
        // Resolve through the private SDK factory, just as Agent does.
        var factory=typeof(FIInstrumentation.TraceAI).Assembly.GetType("FIInstrumentation.SamplingConfiguration")!;
        var selected=(Sampler)factory.GetMethod("Create",BindingFlags.Static|BindingFlags.NonPublic)!.Invoke(null,new object?[] { .25,null })!;
        var wrapped=(Sampler)factory.GetMethod("Wrap",BindingFlags.Static|BindingFlags.NonPublic)!.Invoke(null,new object[] { selected })!;
        return new ExportSampler(wrapped);
    }

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

    // The pre-fix API is ordinary Activity.Context. Falling back to it keeps these
    // regressions executable on the base revision: failure is missing propagation,
    // not a compilation error caused by a new public API.
    private static ActivityContext PropagationContext(Activity? activity=null)
    {
        var method=typeof(TraceAIAuto).GetMethod("GetPropagationContext",BindingFlags.Public|BindingFlags.Static);
        return method==null?(activity??Activity.Current)?.Context??default:
            (ActivityContext)method.Invoke(null,new object?[] { activity })!;
    }

    [Theory]
    [InlineData("traceidratio")] [InlineData("parentbased_traceidratio")]
    public void OwnedRatioPropagatesThroughStandardW3CInjectionAndRemoteExtraction(string samplerName)
    {
        using var environment=new EnvironmentScope(samplerName,"0.25");
        var upstream=new MemoryExporter();
        TraceAIAuto.Init(options=> {
            options.Exporter=upstream; options.WireCapture=false;
            options.ConfigFile=Path.Combine(Path.GetTempPath(),"traceai-propagation-"+Guid.NewGuid()+".toml");
        });
        Activity? root=null;
        for(var attempt=0;attempt<1000;attempt++)
        {
            var candidate=TraceAIAuto.Source.StartActivity("upstream",ActivityKind.Internal,default(ActivityContext));
            if(candidate?.IsAllDataRequested==true) { root=candidate; break; }
            candidate?.Dispose();
        }
        Assert.NotNull(root);
        var headers=new Dictionary<string,string>();
        var propagator=new TraceContextPropagator();
        ActivityTraceId traceId; ActivitySpanId parentId;
        using(root)
        {
            root.TraceStateString="vendor=present";
            traceId=root.TraceId; parentId=root.SpanId;
            using(var unrelated=new Activity("application-unobserved").SetIdFormat(ActivityIdFormat.W3C).Start())
                Assert.Equal(unrelated.Context,PropagationContext(unrelated));
            var context=PropagationContext();
            Assert.Equal(context,PropagationContext(root));
            // A separate context is explicitly selected for this agent-owned handoff;
            // the raw application's context, tags and sampled bit remain untouched.
            propagator.Inject(new PropagationContext(context,default),headers,static (carrier,key,value)=>carrier[key]=value);
            Assert.False(root.Recorded); Assert.Null(root.GetTagItem("fi.sampling.ratio"));
            Assert.Equal("vendor=present",root.Context.TraceState);
        }
        TraceAIAuto.Shutdown();
        Assert.Equal(root.Context,PropagationContext(root));
        Assert.Equal("fi=r:0.25,vendor=present",headers["tracestate"]);
        var received=propagator.Extract(default,headers,static (carrier,key)=>carrier.TryGetValue(key,out var value)?new[] { value }:Array.Empty<string>()).ActivityContext;
        Assert.True(received.IsRemote); Assert.True((received.TraceFlags&ActivityTraceFlags.Recorded)!=0);
        Assert.Equal(traceId,received.TraceId); Assert.Equal(parentId,received.SpanId);
        var downstream=new MemoryExporter();
        using(var agent=new Agent(Config(downstream,.75)))
        {
            using(var child=TraceAIAuto.Source.StartActivity("downstream",ActivityKind.Server,received))
            { Assert.NotNull(child); Assert.False(child.Recorded); Assert.Null(child.GetTagItem("fi.sampling.ratio")); }
            Assert.True(agent.Flush(3000));
        }
        var span=Assert.Single(downstream.Spans);
        Assert.True(span.Recorded); Assert.Equal(traceId,span.TraceId); Assert.Equal(parentId,span.ParentSpanId);
        Assert.Equal(.25,span.GetTagItem("fi.sampling.ratio")); Assert.Equal(received.TraceState,span.TraceStateString);
    }

    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void RatioReservationAtLimitOneDoesNotWarnThatConfigurationIsZero(bool fallback)
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var sampler=FractionalSampler();
        var privacy=new AutoProcessor(Config(new MemoryExporter()),sampler:sampler);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource("test.ratio.warning").SetSampler(sampler).AddProcessor(privacy).Build();
        using var source=new ActivitySource("test.ratio.warning");
        var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"fi=r:0.5",true);
        using var span=source.StartActivity("chat",ActivityKind.Internal,parent);
        Assert.NotNull(span); span.SetTag("gen_ai.operation.name","chat"); span.Stop();
        var previous=Console.Error;
        using var errors=new StringWriter();
        try
        {
            AgentStartupTests.ClearWarnings(); Console.SetError(errors);
            object? Safe(string key,object value,string? operation)=>fallback?throw new InvalidDataException():value;
            var snapshot=ExportSnapshot.Create([span],OpenTelemetry.Resources.Resource.Empty,Safe,text=>text,attributeLimit:1,
                sampled:sampler.Export,samplingMetadata:sampler.ApplyMetadata);
            Assert.Equal(.5,Assert.Single(Assert.Single(snapshot.Spans).TagObjects).Value);
            Assert.DoesNotContain(errors.ToString().Split('\n'),line=>line.StartsWith("[traceAI auto]",StringComparison.Ordinal)&&line.Contains("Span attribute limit is zero"));
            // A genuinely configured zero still produces the traceAI warning.
            var zero=ExportSnapshot.Create([span],OpenTelemetry.Resources.Resource.Empty,Safe,text=>text,attributeLimit:0,
                sampled:sampler.Export,samplingMetadata:sampler.ApplyMetadata);
            Assert.Empty(Assert.Single(zero.Spans).TagObjects);
            Assert.Single(errors.ToString().Split('\n'),line=>line.StartsWith("[traceAI auto]",StringComparison.Ordinal)&&line.Contains("Span attribute limit is zero"));
        }
        finally { Console.SetError(previous); AgentStartupTests.ClearWarnings(); }
    }

    [Theory]
    [InlineData(false)] [InlineData(true)]
    public void RatioMetadataSurvivesNormalAndPrivacyFallbackCopiesWithoutStampingRawSpans(bool fallback)
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var sampler=FractionalSampler();
        var privacy=new AutoProcessor(Config(new MemoryExporter()),sampler:sampler);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource("test.ratio.snapshot").SetSampler(sampler).AddProcessor(privacy).Build();
        using var source=new ActivitySource("test.ratio.snapshot");
        var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"vendor=opaque,fi=r:0.5",true);
        using var span=source.StartActivity("chat",ActivityKind.Internal,parent);
        Assert.NotNull(span); Assert.False(span.Recorded); Assert.Null(span.GetTagItem("fi.sampling.ratio"));
        span.SetTag("output.value","private");
        using(var child=source.StartActivity("child"))
        {
            Assert.NotNull(child); Assert.Null(child.GetTagItem("fi.sampling.ratio"));
            child.SetTag("output.value","private child");
            var context=privacy.SamplingContext(child);
            Assert.Contains("fi=r:0.5",context.TraceState);
            object? Safe(string key,object value,string? operation)=>fallback?throw new InvalidDataException():value;
            var snapshot=ExportSnapshot.Create([child,span],OpenTelemetry.Resources.Resource.Empty,Safe,text=>text,
                sampled:sampler.Export,samplingMetadata:sampler.ApplyMetadata);
            Assert.Equal(2,snapshot.Spans.Length);
            Assert.All(snapshot.Spans,copy=> {
                Assert.True(copy.Recorded); Assert.Equal(.5,copy.GetTagItem("fi.sampling.ratio"));
                Assert.Contains("fi=r:0.5",copy.TraceStateString);
            });
            Assert.False(child.Recorded); Assert.Null(child.GetTagItem("fi.sampling.ratio"));
        }
        Assert.False(span.Recorded); Assert.Null(span.GetTagItem("fi.sampling.ratio"));
    }

    [Theory]
    [InlineData(false,0)] [InlineData(true,0)]
    [InlineData(false,1)] [InlineData(true,1)]
    [InlineData(false,3)] [InlineData(true,3)]
    [InlineData(false,8)] [InlineData(false,9)] [InlineData(false,10)]
    public void RatioReservesAnExportAttributeSlotWithoutSplittingContentGroups(bool fallback,int limit)
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var sampler=FractionalSampler();
        var privacy=new AutoProcessor(Config(new MemoryExporter()),sampler:sampler);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource("test.ratio.budget").SetSampler(sampler).AddProcessor(privacy).Build();
        using var source=new ActivitySource("test.ratio.budget");
        var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"vendor=opaque,fi=r:0.5",true);
        using var span=source.StartActivity("chat",ActivityKind.Internal,parent);
        Assert.NotNull(span);
        span.SetCustomProperty(AutoProcessor.ContextProperty,null);
        span.SetTag("gen_ai.operation.name","chat");
        span.SetTag("gen_ai.provider.name","test");
        span.SetTag("gen_ai.request.model","test-model");
        span.SetTag("output.value","answer"); span.SetTag("output.mime_type","text/plain");
        span.SetTag("input.value","question"); span.SetTag("input.mime_type","text/plain");
        span.SetTag("fi.sampling.ratio",.9); // An application value must not replace the trusted ratio.
        span.Stop();
        object? Safe(string key,object value,string? operation)=>fallback&&key=="output.value"?throw new InvalidDataException():value;
        var snapshot=ExportSnapshot.Create([span],OpenTelemetry.Resources.Resource.Empty,Safe,text=>text,attributeLimit:limit,
            sampled:sampler.Export,samplingMetadata:sampler.ApplyMetadata);
        var copy=Assert.Single(snapshot.Spans);
        Assert.True(copy.Recorded); Assert.Contains("fi=r:0.5",copy.TraceStateString);
        if(fallback) Assert.DoesNotContain("vendor=opaque",copy.TraceStateString);
        Assert.InRange(copy.TagObjects.Count(),0,limit);
        var encoded=Assert.Single(Otlp.Spans(CompatibilityOtlpTransport.Encode(snapshot.Resource,snapshot.Spans,limit)));
        Assert.Equal(copy.TagObjects.Count(),encoded.Attributes.Count);
        Assert.Equal(0UL,encoded.DroppedAttributes);
        if(limit==0) { Assert.Empty(encoded.Attributes); Assert.Null(copy.GetTagItem("fi.sampling.ratio")); }
        else { Assert.Equal(.5,copy.GetTagItem("fi.sampling.ratio")); Assert.Equal(.5,encoded.Attributes["fi.sampling.ratio"]); }
        foreach(var direction in new[] { "input","output" })
            Assert.Equal(encoded.Attributes.ContainsKey(direction+".value"),encoded.Attributes.ContainsKey(direction+".mime_type"));
        if(!fallback&&limit is 8 or 9)
        {
            Assert.Equal(limit==8?4L:2L,encoded.Attributes[AttributeProjection.DroppedContent]);
            Assert.Equal(limit==9,encoded.Attributes.ContainsKey("output.value"));
            Assert.False(encoded.Attributes.ContainsKey("input.value"));
        }
        if(!fallback&&limit==10)
        {
            Assert.True(encoded.Attributes.ContainsKey("input.value")); Assert.True(encoded.Attributes.ContainsKey("output.value"));
            Assert.False(encoded.Attributes.ContainsKey(AttributeProjection.DroppedContent));
        }
        Assert.False(span.Recorded); Assert.Equal(.9,span.GetTagItem("fi.sampling.ratio"));
    }

    [Theory]
    [InlineData(true)] [InlineData(false)]
    public void SamplingContextMergesCachedRatioIntoCurrentVendorTraceState(bool ratioKnown)
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var sampler=FractionalSampler();
        var privacy=new AutoProcessor(Config(new MemoryExporter()),sampler:sampler);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource("test.ratio.state").SetSampler(sampler).AddProcessor(privacy).Build();
        using var source=new ActivitySource("test.ratio.state");
        var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,
            ratioKnown?"vendor=old,fi=r:0.5":"vendor=old",true);
        using var span=source.StartActivity("parent",ActivityKind.Internal,parent);
        Assert.NotNull(span); Assert.False(span.Recorded);
        // OnStart has cached the sampling decision; instrumentation subsequently updates vendors.
        span.TraceStateString="vendor=new,other=added";
        var context=privacy.SamplingContext(span);
        string expected=ratioKnown?"fi=r:0.5,vendor=new,other=added":"vendor=new,other=added";
        Assert.Equal(expected,context.TraceState); Assert.True((context.TraceFlags&ActivityTraceFlags.Recorded)!=0);
        foreach(bool explicitContext in new[] { false,true })
        {
            using var child=explicitContext?source.StartActivity("deferred",ActivityKind.Internal,context):source.StartActivity("ambient");
            Assert.NotNull(child); Assert.False(child.Recorded); Assert.Equal(span.SpanId,child.ParentSpanId);
            Assert.Equal(expected,privacy.SamplingContext(child).TraceState);
            var snapshot=ExportSnapshot.Create([child],OpenTelemetry.Resources.Resource.Empty,(_,value,_)=>value,text=>text,
                sampled:sampler.Export,samplingMetadata:sampler.ApplyMetadata);
            var copy=Assert.Single(snapshot.Spans);
            Assert.Equal(ratioKnown?(object).5:null,copy.GetTagItem("fi.sampling.ratio"));
            Assert.Equal(expected,copy.TraceStateString);
        }
        Assert.Equal("vendor=new,other=added",span.TraceStateString);
        Assert.False(span.Recorded); Assert.Null(span.GetTagItem("fi.sampling.ratio"));
    }

    [Fact]
    public void AgentRatioRootAndLocalChildrenCarryMetadataOnlyOnExportCopies()
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        // Retry random roots until one is recorded; the decision remains the SDK's.
        for(var attempt=0;attempt<1000;attempt++)
        {
            using var root=TraceAIAuto.Source.StartActivity("root",ActivityKind.Internal,default(ActivityContext));
            if(root?.IsAllDataRequested!=true) continue;
            Assert.False(root.Recorded); Assert.Null(root.GetTagItem("fi.sampling.ratio"));
            using(var child=TraceAIAuto.Source.StartActivity("child"))
            { Assert.NotNull(child); Assert.False(child.Recorded); Assert.Null(child.GetTagItem("fi.sampling.ratio")); }
            break;
        }
        Assert.True(agent.Flush(3000)); Assert.Equal(2,exporter.Spans.Count);
        Assert.All(exporter.Spans,span=> { Assert.True(span.Recorded); Assert.Equal(.25,span.GetTagItem("fi.sampling.ratio")); Assert.Contains("fi=r:0.25",span.TraceStateString); });
    }

    [Theory]
    [InlineData("fi=r:0.5", .5)] [InlineData("vendor=opaque", null)]
    [InlineData("fi=r:abc", null)] [InlineData("fi=r:2", null)]
    public void AgentSampledRemoteParentsDoNotUseTheLocalConfiguredRatio(string state,double? expected)
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,state,true);
        using(TraceAIAuto.Source.StartActivity("child",ActivityKind.Internal,parent)) { }
        Assert.True(agent.Flush(3000)); Assert.Equal(expected,Assert.Single(exporter.Spans).GetTagItem("fi.sampling.ratio"));
    }

    [Theory]
    [InlineData(null,null,null)]
    [InlineData("parentbased_always_on",null,null)]
    [InlineData(null,null,"1")]
    [InlineData("parentbased_traceidratio","0",null)]
    [InlineData("parentbased_traceidratio","0.25",null)]
    [InlineData("parentbased_traceidratio","1",null)]
    [InlineData("parentbased_always_off",null,null)]
    public void ParentBasedFactoriesCopyRemoteRatioToExportedLocalDescendants(string? name,string? arg,string? rate)
    {
        using var environment=new EnvironmentScope(name,arg,rate);
        var exporter=new MemoryExporter();
        TraceAIAuto.Init(options=> {
            options.Exporter=exporter; options.WireCapture=false;
            options.ConfigFile=Path.Combine(Path.GetTempPath(),"traceai-copy-ratio-"+Guid.NewGuid()+".toml");
        });
        var headers=new Dictionary<string,string> {
            ["traceparent"]="00-00000000000000010000000000000001-0000000000000001-01",
            ["tracestate"]="fi=r:0.5,vendor=opaque"
        };
        var remote=new TraceContextPropagator().Extract(default,headers,
            static (carrier,key)=>carrier.TryGetValue(key,out var value)?new[] { value }:Array.Empty<string>()).ActivityContext;
        Assert.True(remote.IsRemote);
        Activity server;
        Activity child;
        using(server=TraceAIAuto.Source.StartActivity("server",ActivityKind.Server,remote)!)
        {
            Assert.NotNull(server);
            using(child=TraceAIAuto.Source.StartActivity("local-child")!) Assert.NotNull(child);
            foreach(var raw in new[] { server,child })
            {
                Assert.False(raw.Recorded); Assert.Null(raw.GetTagItem("fi.sampling.ratio"));
                Assert.Equal(remote.TraceState,raw.TraceStateString);
            }
            var context=PropagationContext(server);
            Assert.True((context.TraceFlags&ActivityTraceFlags.Recorded)!=0);
            Assert.Equal(remote.TraceState,context.TraceState);
        }
        Assert.True(TraceAIAuto.Flush(3000));
        Assert.Equal(2,exporter.Spans.Count);
        Assert.All(exporter.Spans,copy=> {
            Assert.True(copy.Recorded); Assert.Equal(.5,copy.GetTagItem("fi.sampling.ratio"));
            Assert.Equal(remote.TraceState,copy.TraceStateString); Assert.Equal(remote.TraceId,copy.TraceId);
        });
        Assert.Equal(remote.SpanId,Assert.Single(exporter.Spans,copy=>copy.DisplayName=="server").ParentSpanId);
        Assert.Equal(server.SpanId,Assert.Single(exporter.Spans,copy=>copy.DisplayName=="local-child").ParentSpanId);
        TraceAIAuto.Shutdown();
    }

    [Theory]
    [InlineData(null,null,null)]
    [InlineData("parentbased_always_on",null,null)]
    [InlineData(null,null,"1")]
    [InlineData("parentbased_traceidratio","1",null)]
    public void FullRateParentBasedRootsDoNotInventRatioAndUnsampledParentsDoNotExport(string? name,string? arg,string? rate)
    {
        using var environment=new EnvironmentScope(name,arg,rate);
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        using(TraceAIAuto.Source.StartActivity("root",ActivityKind.Internal,default(ActivityContext))) { }
        foreach(bool remote in new[] { true,false })
        {
            var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.None,"fi=r:0.5",remote);
            using(TraceAIAuto.Source.StartActivity("unsampled-parent",ActivityKind.Internal,parent)) { }
        }
        Assert.True(agent.Flush(3000));
        var root=Assert.Single(exporter.Spans);
        Assert.Equal("root",root.DisplayName); Assert.True(root.Recorded);
        Assert.Null(root.GetTagItem("fi.sampling.ratio")); Assert.Null(root.TraceStateString);
    }

    [Theory]
    [InlineData("always_on",null,true,null)]
    [InlineData("always_off",null,false,null)]
    [InlineData("traceidratio","1",true,null)]
    [InlineData("traceidratio","0",false,null)]
    [InlineData("traceidratio","0.25",true,.25)]
    public void NonParentBasedFactoriesNeverCopyUpstreamRatio(string name,string? arg,bool sampled,double? expected)
    {
        using var environment=new EnvironmentScope(name,arg);
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        foreach(bool remote in new[] { true,false })
        {
            var parent=new ActivityContext(ActivityTraceId.CreateFromString("00000000000000010000000000000001"),
                ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"fi=r:0.5,vendor=opaque",remote);
            using(TraceAIAuto.Source.StartActivity("child",ActivityKind.Internal,parent)) { }
        }
        Assert.True(agent.Flush(3000)); Assert.Equal(sampled?2:0,exporter.Spans.Count);
        Assert.All(exporter.Spans,copy=> {
            Assert.True(copy.Recorded); Assert.Equal(expected,copy.GetTagItem("fi.sampling.ratio"));
            Assert.Equal(expected.HasValue?"fi=r:0.25,vendor=opaque":"fi=r:0.5,vendor=opaque",copy.TraceStateString);
        });
    }

    [Fact]
    public void ApplicationOwnedProviderDoesNotAcquireRatioMetadata()
    {
        using var environment=new EnvironmentScope(rate:"0.25");
        var exporter=new MemoryExporter(); var hostExporter=new MemoryExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).SetSampler(new AlwaysOnSampler())
            .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
        TraceAIAuto.Init(options=> {
            options.Exporter=exporter; options.Provider=provider; options.SampleRate=.25; options.WireCapture=false;
            options.ConfigFile=Path.Combine(Path.GetTempPath(),"traceai-host-propagation-"+Guid.NewGuid()+".toml");
        });
        var parent=new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,"fi=r:0.5",true);
        using(var activity=TraceAIAuto.Source.StartActivity("host",ActivityKind.Internal,parent))
        {
            Assert.NotNull(activity); Assert.Null(activity.GetTagItem("fi.sampling.ratio"));
            Assert.Equal(activity.Context,PropagationContext(activity));
            var headers=new Dictionary<string,string>();
            new TraceContextPropagator().Inject(new PropagationContext(PropagationContext(activity),default),headers,static (carrier,key,value)=>carrier[key]=value);
            Assert.Equal(activity.Id,headers["traceparent"]); Assert.Equal(activity.TraceStateString,headers["tracestate"]);
        }
        Assert.True(TraceAIAuto.Flush(3000));
        Assert.Null(Assert.Single(hostExporter.Spans).GetTagItem("fi.sampling.ratio"));
        Assert.Null(Assert.Single(exporter.Spans).GetTagItem("fi.sampling.ratio"));
        TraceAIAuto.Shutdown();
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
