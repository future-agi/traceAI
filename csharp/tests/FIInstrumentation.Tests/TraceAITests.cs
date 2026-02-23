namespace FIInstrumentation.Tests;

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
}
