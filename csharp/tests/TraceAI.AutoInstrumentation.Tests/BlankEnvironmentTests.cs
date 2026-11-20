using System.Text;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>A blank (empty or whitespace) FI_* environment value counts as not set only where "not set" is the cautious
/// reading: FI_BASE_URL stops the agent, FI_CONFIG_FILE reads no file and FI_WIRE_CAPTURE is off.</summary>
public class BlankEnvironmentTests
{
    private const string Response = "{\"id\":\"local-id\",\"model\":\"local-test\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello back\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";
    private const string BaseUrlLine = "[traceAI auto] FI_BASE_URL is set but empty; the agent did not start and no traces are sent. Unset it or give it a URL.";

    private sealed class NullExporter : OpenTelemetry.BaseExporter<System.Diagnostics.Activity>
    {
        public override OpenTelemetry.ExportResult Export(in OpenTelemetry.Batch<System.Diagnostics.Activity> batch) => OpenTelemetry.ExportResult.Success;
    }

    private static T With<T>(Func<T> run, params (string Key, string? Value)[] env)
    {
        var saved = env.Select(pair => (pair.Key, Old: Environment.GetEnvironmentVariable(pair.Key))).ToArray();
        try { foreach (var (key, value) in env) Environment.SetEnvironmentVariable(key, value); return run(); }
        finally { foreach (var (key, old) in saved) Environment.SetEnvironmentVariable(key, old); }
    }

    private static string TempFile(string toml)
    {
        var file = Path.Combine(Path.GetTempPath(), "traceai-blank-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(file, toml);
        return file;
    }

    // ---- FI_BASE_URL: blank stops the agent ---------------------------------------------------------------------

    [Theory]
    [InlineData("", true)]
    [InlineData("   ", true)]
    [InlineData("", false)]
    [InlineData("   ", false)]
    public void ABlankBaseUrlRefusesTheConfigurationWhateverTheFileOrDefaultSays(string blank, bool fileHasBaseUrl)
    {
        var file = TempFile(fileHasBaseUrl ? "base_url = \"http://from-file.example\"\n" : "");
        try
        {
            Assert.ThrowsAny<ArgumentException>(() => With(() => new AutoConfig(new AutoOptions { ConfigFile = file }), ("FI_BASE_URL", blank), ("FI_API_KEY", "k"), ("FI_SECRET_KEY", "s")));
        }
        finally { File.Delete(file); }
    }

    [Fact]
    public void ABlankBaseUrlDoesNotRefuseAnExplicitUrlOrASuppliedDestination()
    {
        var file = TempFile("");
        try
        {
            With(() => new AutoConfig(new AutoOptions { ConfigFile = file, BaseUrl = "http://explicit.example" }).Get("BASE_URL"), ("FI_BASE_URL", ""), ("FI_API_KEY", "k"), ("FI_SECRET_KEY", "s"));
            With(() => new AutoConfig(new AutoOptions { ConfigFile = file, Exporter = new NullExporter() }), ("FI_BASE_URL", ""));
        }
        finally { File.Delete(file); }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ABlankBaseUrlStopsTheAgentWithOneLineAndNothingIsExported(bool fileHasBaseUrl)
    {
        await using var server = new MockServer(Encoding.UTF8.GetBytes(Response), "application/json");
        var file = TempFile(fileHasBaseUrl ? $"base_url = \"{server.Url}\"\n" : "");
        try
        {
            var result = await StartupHookTests.RunApp(server.Url + "v1/chat/completions", "json", new Dictionary<string, string?> {
                ["FI_BASE_URL"] = "", ["FI_API_KEY"] = "test-key", ["FI_SECRET_KEY"] = "test-secret", ["FI_CONFIG_FILE"] = file });
            Assert.Equal(0, result.ExitCode);
            Assert.Equal(Response, Encoding.UTF8.GetString(Convert.FromBase64String(result.Output.Trim())));
            Assert.Empty(server.Exports);
            Assert.Equal(BaseUrlLine, result.Error.Trim());
            Assert.DoesNotContain(server.Url, result.Error);
        }
        finally { File.Delete(file); }
    }

    // ---- FI_CONFIG_FILE: blank reads no file ---------------------------------------------------------------------

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankConfigFileVariableReadsNoFileNotEvenTheOneInTheWorkingDirectory(string blank)
    {
        var directory = Path.Combine(Path.GetTempPath(), "traceai-cwd-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        File.WriteAllText(Path.Combine(directory, "futureagi.toml"), "project_name = \"from-working-directory\"\n");
        var previous = Environment.CurrentDirectory;
        try
        {
            Environment.CurrentDirectory = directory;
            var blankName = With(() => new AutoConfig(new AutoOptions()).Get("PROJECT_NAME"), ("FI_CONFIG_FILE", blank), ("FI_PROJECT_NAME", null));
            Assert.NotEqual("from-working-directory", blankName);
            // Unset keeps what it always did: the working directory's file is read.
            var unsetName = With(() => new AutoConfig(new AutoOptions()).Get("PROJECT_NAME"), ("FI_CONFIG_FILE", null), ("FI_PROJECT_NAME", null));
            Assert.Equal("from-working-directory", unsetName);
        }
        finally { Environment.CurrentDirectory = previous; Directory.Delete(directory, true); }
    }

    // ---- FI_WIRE_CAPTURE: blank is off --------------------------------------------------------------------------

    [Theory]
    [InlineData("", "")]
    [InlineData("   ", "")]
    [InlineData("", "wire_capture = true\n")]
    [InlineData("   ", "wire_capture = true\n")]
    public void ABlankWireCaptureIsOffWithOrWithoutAFileSetting(string blank, string toml)
    {
        var file = TempFile(toml);
        try
        {
            var config = With(() => new AutoConfig(new AutoOptions { ConfigFile = file }), ("FI_WIRE_CAPTURE", blank), ("FI_API_KEY", "k"), ("FI_SECRET_KEY", "s"));
            Assert.False(config.Bool("WIRE_CAPTURE"));
            Assert.False(config.Enabled("wire"));
        }
        finally { File.Delete(file); }
    }

    [Theory]
    [InlineData("")]
    [InlineData("wire_capture = true\n")]
    public async Task ABlankWireCaptureExportsNoWireSpan(string toml)
    {
        await using var server = new MockServer(Encoding.UTF8.GetBytes(Response), "application/json");
        var file = TempFile(toml);
        try
        {
            var result = await StartupHookTests.RunApp(server.Url + "v1/chat/completions", "json", new Dictionary<string, string?> {
                ["FI_WIRE_CAPTURE"] = "", ["FI_BASE_URL"] = server.Url, ["FI_API_KEY"] = "test-key", ["FI_SECRET_KEY"] = "test-secret", ["FI_CONFIG_FILE"] = file });
            Assert.Equal(0, result.ExitCode);
            Assert.DoesNotContain(server.Exports.SelectMany(Otlp.Spans), s => s.Attributes.GetValueOrDefault("traceai.capture.layer")?.ToString() == "wire");
        }
        finally { File.Delete(file); }
    }

    // ---- keys where a blank value is the same as unset (earlier rule kept) -------------------------------------

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankDisabledInstrumentationsLetsTheFileListApply(string blank)
    {
        var file = TempFile("disabled_instrumentations = [\"agents\"]\n");
        try
        {
            var plan = With(() => TraceAIAuto.HostingStartupPlanFor(new AutoConfig(new AutoOptions { ConfigFile = file })), ("FI_DISABLED_INSTRUMENTATIONS", blank));
            Assert.Equal("chat=1;agent=0;sensitive=1", plan);
        }
        finally { File.Delete(file); }
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankSampleRateIsNotSetSoTheStandardSamplerApplies(string blank)
    {
        var none = TempFile("");
        var set = TempFile("sample_rate = 0.5\n");
        try
        {
            Assert.False(With(() => new AutoConfig(new AutoOptions { ConfigFile = none }).HasSampleRate, ("FI_SAMPLE_RATE", blank)));
            var fromFile = With(() => new AutoConfig(new AutoOptions { ConfigFile = set }), ("FI_SAMPLE_RATE", blank));
            Assert.True(fromFile.HasSampleRate);
            Assert.Equal(0.5, fromFile.SampleRate);
        }
        finally { File.Delete(none); File.Delete(set); }
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankProjectTypeIsTheFileValueOrObserve(string blank)
    {
        var none = TempFile("");
        var set = TempFile("project_type = \"experiment\"\n");
        try
        {
            Assert.Equal("observe", With(() => new AutoConfig(new AutoOptions { ConfigFile = none }).Get("PROJECT_TYPE"), ("FI_PROJECT_TYPE", blank)));
            Assert.Equal("experiment", With(() => new AutoConfig(new AutoOptions { ConfigFile = set }).Get("PROJECT_TYPE"), ("FI_PROJECT_TYPE", blank)));
        }
        finally { File.Delete(none); File.Delete(set); }
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void ABlankAutoDebugLetsTheFileDecide(string blank)
    {
        var file = TempFile("debug = true\n");
        try { Assert.True(With(() => new AutoConfig(new AutoOptions { ConfigFile = file }).Bool("AUTO_DEBUG"), ("FI_AUTO_DEBUG", blank))); }
        finally { File.Delete(file); }
    }
}
