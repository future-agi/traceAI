using System.Diagnostics;
using OpenTelemetry.Trace;

namespace FIInstrumentation.Tests;

[CollectionDefinition("Sampler environment", DisableParallelization = true)]
public class SamplerEnvironmentCollection { }

[Collection("Sampler environment")]
public class TraceAITests : IDisposable
{
    public TraceAITests()
    {
        // Ensure clean state before each test
        TraceAI.Shutdown();
    }

    public void Dispose()
    {
        TraceAI.Shutdown();
    }

    [Fact]
    public void Register_ReturnsFITracer()
    {
        var tracer = TraceAI.Register(opts =>
        {
            opts.ProjectName = "test-project";
            opts.Verbose = false;
            opts.EnableConsoleExporter = false;
            // Don't set endpoint/apiKey to avoid real HTTP calls
        });

        Assert.NotNull(tracer);
        Assert.NotNull(tracer.ActivitySource);
        Assert.NotNull(tracer.Config);
    }

    [Fact]
    public void Register_ReturnsSameInstance_WhenCalledTwice()
    {
        var tracer1 = TraceAI.Register(opts =>
        {
            opts.ProjectName = "test-project";
            opts.Verbose = false;
        });

        var tracer2 = TraceAI.Register(opts =>
        {
            opts.ProjectName = "different-project";
            opts.Verbose = false;
        });

        Assert.Same(tracer1, tracer2);
    }

    [Fact]
    public void GetTracer_ReturnsNull_BeforeRegister()
    {
        Assert.Null(TraceAI.GetTracer());
    }

    [Fact]
    public void GetTracer_ReturnsTracer_AfterRegister()
    {
        TraceAI.Register(opts =>
        {
            opts.ProjectName = "test";
            opts.Verbose = false;
        });

        Assert.NotNull(TraceAI.GetTracer());
    }

    [Fact]
    public void Shutdown_ClearsTracer()
    {
        TraceAI.Register(opts =>
        {
            opts.ProjectName = "test";
            opts.Verbose = false;
        });

        TraceAI.Shutdown();
        Assert.Null(TraceAI.GetTracer());
    }

    [Fact]
    public void Register_WithCustomTraceConfig()
    {
        var tracer = TraceAI.Register(opts =>
        {
            opts.ProjectName = "masked-project";
            opts.Verbose = false;
            opts.TraceConfig = TraceConfig.Builder()
                .HideInputs()
                .HideOutputs()
                .Build();
        });

        Assert.True(tracer.Config.HideInputs);
        Assert.True(tracer.Config.HideOutputs);
    }

    private sealed class SamplerEnvironment : IDisposable
    {
        private readonly Dictionary<string,string?> _previous = new();
        internal SamplerEnvironment(string? name = null, string? arg = null, string? rate = null)
        {
            Set("OTEL_TRACES_SAMPLER", name); Set("OTEL_TRACES_SAMPLER_ARG", arg); Set("FI_SAMPLE_RATE", rate);
        }
        private void Set(string name, string? value)
        { _previous[name] = Environment.GetEnvironmentVariable(name); Environment.SetEnvironmentVariable(name, value); }
        public void Dispose()
        { foreach (var pair in _previous) Environment.SetEnvironmentVariable(pair.Key, pair.Value); }
    }

    private static FITracer RegisterForSampling(Action<RegisterOptions>? configure = null) => TraceAI.Register(options =>
    {
        options.Verbose = false;
        options.Endpoint = "http://127.0.0.1:1";
        configure?.Invoke(options);
    });

    // Clear Recorded before stopping so these provider tests never send an OTLP request.
    private static void AssertSampled(FITracer tracer, bool expected, ActivityContext parent = default)
    {
        using var activity = tracer.ActivitySource.StartActivity("sampling", ActivityKind.Internal, parent);
        try { Assert.Equal(expected, activity?.Recorded ?? false); }
        finally { if (activity != null) activity.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded; }
    }

    [Fact]
    public void Register_CustomSamplerOverridesFiAndOtelEnvironment()
    {
        using var environment = new SamplerEnvironment("always_on", rate: "1");
        var sampler = new CountingDropSampler();
        var tracer = RegisterForSampling(options =>
        {
            // Reflection keeps the RED test compilable against the base without this option.
            var property = typeof(RegisterOptions).GetProperty("Sampler");
            Assert.NotNull(property); Assert.Equal(typeof(Sampler), property.PropertyType);
            property.SetValue(options, sampler);
        });
        AssertSampled(tracer, false); Assert.Equal(1, sampler.Calls);
        Assert.Same(tracer, TraceAI.Register(_ => throw new InvalidOperationException("already initialized")));
        AssertSampled(tracer, false); Assert.Equal(2, sampler.Calls);
    }

    [Theory]
    [InlineData(true, .25)] [InlineData(false, .25)]
    [InlineData(true, 1)] [InlineData(false, 1)]
    public void RegisterSamplerOptionStampsOnlyFactoryProducedRatios(bool known, double rate)
    {
        using var environment = new SamplerEnvironment("always_off");
        Sampler sampler = known ? SamplingConfiguration.Create(rate) : new ParentBasedSampler(new TraceIdRatioBasedSampler(rate));
        var tracer = RegisterForSampling(options => options.Sampler = sampler);
        var parent = new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded, "fi=r:0.5,vendor=opaque", true);
        using var span = tracer.ActivitySource.StartActivity("ratio", ActivityKind.Internal, parent);
        try
        {
            Assert.NotNull(span); Assert.True(span.Recorded);
            Assert.Equal(known ? (object).5 : null, span.GetTagItem("fi.sampling.ratio"));
            Assert.Contains("vendor=opaque", span.TraceStateString);
        }
        finally { if (span != null) span.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded; }
    }

    [Theory]
    [InlineData("traceidratio", "0.25", true)]
    [InlineData("parentbased_traceidratio", "0.25", false)]
    [InlineData("traceidratio", "1", false)]
    [InlineData("always_on", null, false)]
    [InlineData("parentbased_always_on", null, false)]
    public void RegisterEnvironmentStampsOnlyDecisionsMadeByOurRatio(string name, string? arg, bool stamped)
    {
        using var environment = new SamplerEnvironment(name, arg);
        var tracer = RegisterForSampling();
        var parent = new ActivityContext(ActivityTraceId.CreateFromString("00000000000000010000000000000001"), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded, "vendor=opaque", true);
        using var span = tracer.ActivitySource.StartActivity("ratio", ActivityKind.Internal, parent);
        try
        {
            Assert.NotNull(span); Assert.True(span.Recorded);
            Assert.Equal(stamped ? (object).25 : null, span.GetTagItem("fi.sampling.ratio"));
            Assert.Equal(stamped ? "fi=r:0.25,vendor=opaque" : "vendor=opaque", span.TraceStateString);
        }
        finally { if (span != null) span.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded; }
    }

    [Theory]
    [InlineData(null, null, null)]
    [InlineData("parentbased_always_on", null, null)]
    [InlineData(null, null, "1")]
    [InlineData("parentbased_traceidratio", "0", null)]
    [InlineData("parentbased_traceidratio", "1", null)]
    [InlineData("parentbased_always_off", null, null)]
    public void RegisterParentBasedFactoriesCopyRemoteAndLocalParentRatio(string? name, string? arg, string? rate)
    {
        using var environment = new SamplerEnvironment(name, arg, rate);
        var tracer = RegisterForSampling();
        var parent = new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded, "fi=r:0.5,vendor=opaque", true);
        using var server = tracer.ActivitySource.StartActivity("server", ActivityKind.Server, parent);
        try
        {
            Assert.NotNull(server); Assert.True(server.Recorded);
            Assert.Equal(.5, server.GetTagItem("fi.sampling.ratio"));
            Assert.Equal(parent.TraceState, server.TraceStateString);
            using var child = tracer.ActivitySource.StartActivity("local-child");
            try
            {
                Assert.NotNull(child); Assert.True(child.Recorded);
                Assert.Equal(server.SpanId, child.ParentSpanId);
                Assert.Equal(.5, child.GetTagItem("fi.sampling.ratio"));
                Assert.Equal(parent.TraceState, child.TraceStateString);
            }
            finally { if (child != null) child.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded; }
        }
        finally { if (server != null) server.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded; }
    }

    private sealed class CountingDropSampler : Sampler
    {
        internal int Calls;
        public override SamplingResult ShouldSample(in SamplingParameters parameters)
        { Calls++; return new SamplingResult(SamplingDecision.Drop); }
    }

    [Theory]
    [InlineData("always_off", null)]
    [InlineData("traceidratio", "0")]
    [InlineData("parentbased_traceidratio", "0")]
    public void Register_AbsentSamplerHonoursEnvironment(string name, string? arg)
    {
        using var environment = new SamplerEnvironment(name, arg);
        AssertSampled(RegisterForSampling(), false);
    }

    [Fact]
    public void Register_AbsentSamplerKeepsDefaultParentBasedOne()
    {
        using var environment = new SamplerEnvironment();
        var tracer = RegisterForSampling();
        AssertSampled(tracer, true);
        foreach (var remote in new[] { true, false })
        {
            AssertSampled(tracer, false, new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.None, isRemote: remote));
            AssertSampled(tracer, true, new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded, isRemote: remote));
        }
    }

    [Theory]
    [InlineData("always_on", "0", false)]
    [InlineData("always_off", "1", true)]
    public void Register_FiSampleRateWinsOverOtel(string name, string rate, bool expected)
    {
        using var environment = new SamplerEnvironment(name, rate: rate);
        AssertSampled(RegisterForSampling(), expected);
    }

    [Theory]
    [InlineData("unknown-sampler")]
    [InlineData(" always_off ")]
    public void Register_InvalidEnvironmentFallsBackWithOneWarning(string name)
    {
        using var environment = new SamplerEnvironment(name);
        using var errors = new StringWriter(); var previous = Console.Error;
        try
        {
            Console.SetError(errors);
            var tracer = RegisterForSampling(); AssertSampled(tracer, true); AssertSampled(tracer, true);
            foreach (var remote in new[] { true, false })
            {
                AssertSampled(tracer, false, new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.None, isRemote: remote));
                AssertSampled(tracer, true, new ActivityContext(ActivityTraceId.CreateRandom(), ActivitySpanId.CreateRandom(), ActivityTraceFlags.Recorded, isRemote: remote));
            }
            Assert.Single(errors.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries), line => line.Contains("OTEL_TRACES_SAMPLER"));
            Assert.DoesNotContain(name, errors.ToString());
        }
        finally { Console.SetError(previous); }
    }

    [Fact]
    public void Register_DroppedSpansDoNotInterruptApplicationOrCaptureAttributes()
    {
        using var environment = new SamplerEnvironment("always_off");
        var tracer = RegisterForSampling();
        var result = tracer.Chain("dropped", span =>
        {
            try
            {
                Assert.False(span.Activity.Recorded);
                span.SetInput("not recorded");
                Assert.Empty(span.Activity.TagObjects);
                return 42;
            }
            finally { span.Activity.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded; }
        });
        Assert.Equal(42, result);
    }
}
