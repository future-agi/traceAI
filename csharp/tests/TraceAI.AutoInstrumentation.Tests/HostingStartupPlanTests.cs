using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>What the agent tells the ASP.NET Core hosting startup: which kinds to wrap, and whether the libraries'
/// sensitive-content switch may be on. It is on unless FI_AUTO_CAPTURE_CONTENT is set false, and any hide setting keeps it off.</summary>
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
    public void ContentIsOnByDefault() => Assert.Equal("chat=1;agent=1;sensitive=1", Plan());

    [Theory]
    [InlineData("true")]
    [InlineData("1")]
    [InlineData("yes")]
    [InlineData("on")]
    public void AnExplicitTrueKeepsContentOn(string value) =>
        Assert.Equal("chat=1;agent=1;sensitive=1", Plan(null, ("FI_AUTO_CAPTURE_CONTENT", value)));

    [Theory]
    [InlineData("false")]
    [InlineData("FALSE")]
    [InlineData("0")]
    [InlineData("no")]
    [InlineData("off")]
    public void AnExplicitFalseTurnsContentOff(string value) =>
        Assert.Equal("chat=1;agent=1;sensitive=0", Plan(null, ("FI_AUTO_CAPTURE_CONTENT", value)));

    [Theory]
    [InlineData("HideInputs")]
    [InlineData("HideOutputs")]
    [InlineData("HideInputMessages")]
    [InlineData("HideOutputMessages")]
    [InlineData("HideInputText")]
    [InlineData("HideOutputText")]
    [InlineData("HideInputImages")]
    public void AnyHideSettingKeepsContentOffByDefault(string setting)
    {
        var mask = new FIInstrumentation.TraceConfig();
        typeof(FIInstrumentation.TraceConfig).GetProperty(setting)!.SetValue(mask, true);
        Assert.Equal("chat=1;agent=1;sensitive=0", Plan(o => o.TraceConfig = mask));
    }

    [Theory]
    [InlineData("FI_HIDE_INPUTS")]
    [InlineData("FI_HIDE_OUTPUTS")]
    [InlineData("FI_HIDE_INPUT_MESSAGES")]
    [InlineData("FI_HIDE_OUTPUT_MESSAGES")]
    [InlineData("FI_HIDE_INPUT_TEXT")]
    [InlineData("FI_HIDE_OUTPUT_TEXT")]
    [InlineData("FI_HIDE_INPUT_IMAGES")]
    public void AnyHideVariableKeepsContentOffByDefault(string variable) =>
        Assert.Equal("chat=1;agent=1;sensitive=0", Plan(null, (variable, "true")));

    [Fact]
    public void ASuppliedTraceConfigWinsOverTheHideVariables() =>
        Assert.Equal("chat=1;agent=1;sensitive=1", Plan(o => o.TraceConfig = new FIInstrumentation.TraceConfig(), ("FI_HIDE_INPUTS", "true")));

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
        Assert.Equal("chat=1;agent=0;sensitive=1", Plan(o => o.DisabledInstrumentations = ["agents"]));
        Assert.Equal("chat=0;agent=1;sensitive=1", Plan(o => o.DisabledInstrumentations = ["extensions_ai"]));
        Assert.Null(Plan(o => o.DisabledInstrumentations = ["extensions_ai", "agents"]));
    }
}
