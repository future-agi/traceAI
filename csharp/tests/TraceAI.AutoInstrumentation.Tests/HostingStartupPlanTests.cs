using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>What the agent tells the ASP.NET Core hosting startup: which kinds to wrap, and whether the libraries'
/// sensitive-content switch may be on. It is off unless the user opts in, and any hide setting keeps it off.</summary>
public class HostingStartupPlanTests
{
    private static string? Plan(Action<AutoOptions>? configure = null, params (string Key, string? Value)[] env)
    {
        var saved = env.Select(pair => (pair.Key, Old: Environment.GetEnvironmentVariable(pair.Key))).ToArray();
        try
        {
            foreach (var (key, value) in env) Environment.SetEnvironmentVariable(key, value);
            var options = new AutoOptions { ConfigFile = Path.Combine(Path.GetTempPath(), "traceai-no-such-config.toml") };
            configure?.Invoke(options);
            return TraceAIAuto.HostingStartupPlanFor(new AutoConfig(options));
        }
        finally { foreach (var (key, old) in saved) Environment.SetEnvironmentVariable(key, old); }
    }

    [Fact]
    public void ContentIsOffByDefault() => Assert.Equal("chat=1;agent=1;sensitive=0", Plan());

    [Theory]
    [InlineData("true")]
    [InlineData("1")]
    public void OptingInTurnsContentOn(string value) =>
        Assert.Equal("chat=1;agent=1;sensitive=1", Plan(null, ("FI_AUTO_CAPTURE_CONTENT", value)));

    [Theory]
    [InlineData("HideInputs")]
    [InlineData("HideOutputs")]
    [InlineData("HideInputMessages")]
    [InlineData("HideOutputMessages")]
    [InlineData("HideInputText")]
    [InlineData("HideOutputText")]
    [InlineData("HideInputImages")]
    public void AnyHideSettingKeepsContentOffEvenWhenOptedIn(string setting)
    {
        var mask = new FIInstrumentation.TraceConfig();
        typeof(FIInstrumentation.TraceConfig).GetProperty(setting)!.SetValue(mask, true);
        Assert.Equal("chat=1;agent=1;sensitive=0", Plan(o => o.TraceConfig = mask, ("FI_AUTO_CAPTURE_CONTENT", "true")));
    }

    [Fact]
    public void DisabledIntegrationsAreLeftOutAndNothingEnabledMeansNoPlan()
    {
        Assert.Equal("chat=1;agent=0;sensitive=0", Plan(o => o.DisabledInstrumentations = ["agents"]));
        Assert.Equal("chat=0;agent=1;sensitive=0", Plan(o => o.DisabledInstrumentations = ["extensions_ai"]));
        Assert.Null(Plan(o => o.DisabledInstrumentations = ["extensions_ai", "agents"]));
    }
}
