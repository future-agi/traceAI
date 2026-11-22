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

    // ---- Blank environment values, TOML spellings and unrecognised values ----------------------------------

    private static T WithFile<T>(string toml, Func<string, T> run, params (string Key, string? Value)[] env)
    {
        var file = Path.Combine(Path.GetTempPath(), "traceai-plan-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(file, toml);
        var saved = env.Select(pair => (pair.Key, Old: Environment.GetEnvironmentVariable(pair.Key))).ToArray();
        try
        {
            foreach (var (key, value) in env) Environment.SetEnvironmentVariable(key, value);
            return run(file);
        }
        finally { foreach (var (key, old) in saved) Environment.SetEnvironmentVariable(key, old); File.Delete(file); }
    }

    private static string? PlanFromFile(string toml, params (string Key, string? Value)[] env) =>
        WithFile(toml, file => TraceAIAuto.HostingStartupPlanFor(new AutoConfig(new AutoOptions { ConfigFile = file })), env);

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankEnvironmentValueDoesNotTurnAFileOptOutOn(string blank) =>
        Assert.Equal("chat=1;agent=1;sensitive=0", PlanFromFile("auto_capture_content = false\n", ("FI_AUTO_CAPTURE_CONTENT", blank)));

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankEnvironmentValueWithNoFileSettingIsTheDefault(string blank) =>
        Assert.Equal("chat=1;agent=1;sensitive=1", PlanFromFile("", ("FI_AUTO_CAPTURE_CONTENT", blank)));

    [Theory]
    [InlineData("FI_PROJECT_NAME", "project_name = \"from-file\"\n", "PROJECT_NAME", "from-file")]
    [InlineData("FI_PROJECT_TYPE", "project_type = \"experiment\"\n", "PROJECT_TYPE", "experiment")]
    [InlineData("FI_INSTRUMENTATIONS", "instrumentations = [\"openai\"]\n", "INSTRUMENTATIONS", "openai")]
    public void ABlankEnvironmentValueNeverOverridesTheFile(string variable, string toml, string key, string expected)
    {
        foreach (var blank in new[] { "", "   " })
            Assert.Equal(expected, WithFile(toml, file => new AutoConfig(new AutoOptions { ConfigFile = file }).Get(key), (variable, blank), ("FI_API_KEY", "k"), ("FI_SECRET_KEY", "s")));
    }

    [Theory]
    [InlineData("false")]
    [InlineData("'false'")]
    [InlineData("\"false\"")]
    [InlineData("'off'")]
    [InlineData("'no'")]
    [InlineData("'0'")]
    [InlineData("\"off\"")]
    public void EveryTomlSpellingOfFalseTurnsContentOff(string literal) =>
        Assert.Equal("chat=1;agent=1;sensitive=0", PlanFromFile($"auto_capture_content = {literal}\n"));

    [Theory]
    [InlineData("true")]
    [InlineData("'true'")]
    [InlineData("\"true\"")]
    [InlineData("'on'")]
    public void EveryTomlSpellingOfTrueKeepsContentOn(string literal) =>
        Assert.Equal("chat=1;agent=1;sensitive=1", PlanFromFile($"auto_capture_content = {literal}\n"));

    private static (string? Plan, string Error) PlanWithStderr(params (string Key, string? Value)[] env)
    {
        ((System.Collections.Concurrent.ConcurrentDictionary<string, byte>)typeof(TraceAIAuto)
            .GetField("Warnings", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!.GetValue(null)!).Clear();
        var saved = Console.Error; var writer = new StringWriter(); Console.SetError(writer);
        try { return (Plan(null, env), writer.ToString()); }
        finally { Console.SetError(saved); }
    }

    [Theory]
    [InlineData("enabled")]
    [InlineData("sure")]
    public void AnUnrecognisedValueIsOffWithOneWarningThatNamesOnlyTheSetting(string value)
    {
        // Evaluated twice in one process without clearing the warnings: the warning is printed once.
        ((System.Collections.Concurrent.ConcurrentDictionary<string, byte>)typeof(TraceAIAuto)
            .GetField("Warnings", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!.GetValue(null)!).Clear();
        var saved = Console.Error; var writer = new StringWriter(); Console.SetError(writer);
        string? first, second;
        try { first = Plan(null, ("FI_AUTO_CAPTURE_CONTENT", value)); second = Plan(null, ("FI_AUTO_CAPTURE_CONTENT", value)); }
        finally { Console.SetError(saved); }
        var error = writer.ToString();
        Assert.Equal("chat=1;agent=1;sensitive=0", first);
        Assert.Equal("chat=1;agent=1;sensitive=0", second);
        Assert.Equal(1, error.Split("FI_AUTO_CAPTURE_CONTENT").Length - 1);
        Assert.DoesNotContain(value, error.Replace("FI_AUTO_CAPTURE_CONTENT", ""));
    }

    [Theory]
    [InlineData("true")]
    [InlineData("false")]
    [InlineData("")]
    public void ARecognisedOrBlankValueWarnsNothing(string value) =>
        Assert.Equal("", PlanWithStderr(("FI_AUTO_CAPTURE_CONTENT", value)).Error);
}
