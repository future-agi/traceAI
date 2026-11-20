using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;
using Xunit;

namespace TraceAI.AutoInstrumentation.HostingStartup.Tests;

/// <summary>An ASP.NET Core host with DI-registered AI services and no tracing code of its own, run under the
/// real startup hook against a local collector.</summary>
public class ActivationTests
{
    private const string Existing = "Some.Other;";
    // The stub client's response model appears once in each exported chat span.
    private const string ChatModel = "stub-model";
    private const string Ours = "TraceAI.AutoInstrumentation.HostingStartup";

    private static string Repo()
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir != null; dir = dir.Parent)
            if (Directory.Exists(Path.Combine(dir.FullName, "csharp", "src"))) return dir.FullName;
        throw new DirectoryNotFoundException("repository root");
    }

    private static string Configuration => AppContext.BaseDirectory.Contains("/Release/") ? "Release" : "Debug";

    private sealed record Result(JsonElement Summary, string Exported, string Error);

    private static async Task<Result> Run(string mode, Dictionary<string, string?>? env = null)
    {
        await using var collector = new Collector();
        var csharp = Path.Combine(Repo(), "csharp");
        var host = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") ?? "dotnet";
        var start = new ProcessStartInfo(host) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, WorkingDirectory = csharp };
        start.ArgumentList.Add(Path.Combine(csharp, "tests", "TraceAI.AutoInstrumentation.WebApp", "bin", Configuration, "net10.0", "TraceAI.AutoInstrumentation.WebApp.dll"));
        start.ArgumentList.Add(mode);
        foreach (var key in start.Environment.Keys.Where(k => k.StartsWith("FI_") || k.StartsWith("OTEL_") || k.StartsWith("ASPNETCORE_") || k == "DOTNET_STARTUP_HOOKS").ToArray()) start.Environment.Remove(key);
        start.Environment["DOTNET_STARTUP_HOOKS"] = Path.Combine(csharp, "src", "TraceAI.AutoInstrumentation", "bin", Configuration, "net8.0", "TraceAI.AutoInstrumentation.dll");
        start.Environment["FI_BASE_URL"] = collector.Url;
        start.Environment["FI_API_KEY"] = "test-key";
        start.Environment["FI_SECRET_KEY"] = "test-secret";
        start.Environment["FI_CONFIG_FILE"] = Path.Combine(csharp, "no-such-config.toml");
        start.Environment["ASPNETCORE_HOSTINGSTARTUPASSEMBLIES"] = Existing;
        foreach (var pair in env ?? []) if (pair.Value == null) start.Environment.Remove(pair.Key); else start.Environment[pair.Key] = pair.Value;
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(60));
        var output = await stdout;
        Assert.True(process.ExitCode == 0, output + await stderr);
        var summary = JsonDocument.Parse(output.Trim().Split('\n').Last()).RootElement.Clone();
        return new Result(summary, collector.Payload(), await stderr);
    }

    private static int Count(string haystack, string needle) => haystack.Split(needle).Length - 1;

    [Fact]
    public async Task AHostWithRegisteredServicesIsTracedWithNoApplicationCode()
    {
        var run = await Run("plain");
        Assert.True(run.Summary.GetProperty("wrappedChat").GetBoolean());
        Assert.Equal(1, Count(run.Exported, ChatModel));
        Assert.Equal(1, Count(run.Exported, "EMBEDDING"));
        Assert.Equal(1, Count(run.Exported, "AGENT"));
        Assert.Contains("Experimental.Microsoft.Extensions.AI", run.Exported);
        Assert.Contains("Experimental.Microsoft.Agents.AI", run.Exported);
        var status = run.Summary.GetProperty("integrations");
        Assert.StartsWith("instrumented|auto (web host)", status.GetProperty("extensions_ai").GetString());
        Assert.StartsWith("instrumented|auto (web host)", status.GetProperty("agents").GetString());
    }

    [Fact]
    public async Task AnExistingHostingStartupValueIsPreservedAndOursIsAppended()
    {
        var run = await Run("plain");
        Assert.Equal(Existing + Ours, run.Summary.GetProperty("hostingStartupVariable").GetString());
    }

    [Fact]
    public async Task AnAppThatAlreadyCallsUseOpenTelemetryGetsExactlyOneSpanPerCall()
    {
        var run = await Run("own_otel");
        Assert.Equal(1, Count(run.Exported, ChatModel));
    }

    [Theory]
    [InlineData("FI_DISABLED_INSTRUMENTATIONS", "extensions_ai,agents")]
    [InlineData("OTEL_SDK_DISABLED", "true")]
    public async Task DisablingTheIntegrationOrTheAgentLeavesTheHostUntouched(string key, string value)
    {
        var run = await Run("plain", new() { [key] = value });
        Assert.False(run.Summary.GetProperty("wrappedChat").GetBoolean());
        Assert.Equal(Existing, run.Summary.GetProperty("hostingStartupVariable").GetString());
        Assert.Equal(0, Count(run.Exported, ChatModel));
    }

    [Fact]
    public async Task DisablingOnlyAgentsStillTracesTheChatClient()
    {
        var run = await Run("plain", new() { ["FI_DISABLED_INSTRUMENTATIONS"] = "agents" });
        Assert.Equal(1, Count(run.Exported, ChatModel));
        Assert.DoesNotContain("Experimental.Microsoft.Agents.AI", run.Exported);
    }

    [Theory]
    [InlineData("FI_HIDE_INPUTS")]
    [InlineData("FI_HIDE_OUTPUTS")]
    [InlineData("FI_HIDE_INPUT_TEXT")]
    public async Task ContentCaptureFollowsTheTraceAiHideSettings(string flag)
    {
        var run = await Run("plain", new() { [flag] = "true", ["FI_AUTO_CAPTURE_CONTENT"] = "true" });
        Assert.Equal(1, Count(run.Exported, ChatModel));
        Assert.False(run.Summary.GetProperty("otherListenerSawContent").GetBoolean());
        Assert.DoesNotContain("hello chat", run.Exported);
        Assert.DoesNotContain("stub answer", run.Exported);
    }

    [Fact]
    public async Task AnotherListenerOnTheLibrarySourcesReceivesNoContentWhenCaptureIsSetFalse()
    {
        var run = await Run("plain", new() { ["FI_AUTO_CAPTURE_CONTENT"] = "false" });
        Assert.True(run.Summary.GetProperty("otherListenerSpanTags").GetInt32() > 0, "the application's own listener saw no spans");
        Assert.False(run.Summary.GetProperty("otherListenerSawContent").GetBoolean());
        Assert.DoesNotContain("hello chat", run.Exported);
        Assert.DoesNotContain("FI_AUTO_CAPTURE_CONTENT", run.Error);
    }

    [Fact]
    public async Task ContentIsOnByDefaultForEveryListenerAndTheHideSettingsStillWin()
    {
        var byDefault = await Run("plain");
        Assert.True(byDefault.Summary.GetProperty("otherListenerSawContent").GetBoolean());
        Assert.Contains("hello chat", byDefault.Exported);
        var hidden = await Run("plain", new() { ["FI_HIDE_INPUTS"] = "true" });
        Assert.False(hidden.Summary.GetProperty("otherListenerSawContent").GetBoolean());
        Assert.DoesNotContain("hello chat", hidden.Exported);
        Assert.DoesNotContain("FI_AUTO_CAPTURE_CONTENT", hidden.Error);
    }

    [Fact]
    public async Task ADefaultOnPromptReachesTheCollectorWithASeededSecretReplaced()
    {
        const string secret = "sk-seeded-secret-value-9f8e7d6c5b4a";
        var run = await Run("plain", new() { ["FI_SECRET_KEY"] = secret });
        Assert.Contains("hello chat", run.Exported);
        Assert.DoesNotContain(secret, run.Exported);
        Assert.DoesNotContain(secret, run.Error);
    }

    [Fact]
    public async Task TheContentStartupLineIsPrintedOncePerProcessAndNamesOnlyTheSetting()
    {
        const string secret = "sk-seeded-secret-value-9f8e7d6c5b4a";
        var run = await Run("plain", new() { ["FI_SECRET_KEY"] = secret });
        Assert.Equal(1, Count(run.Error, "FI_AUTO_CAPTURE_CONTENT"));
        Assert.DoesNotContain("hello chat", run.Error);
        Assert.DoesNotContain(secret, run.Error);
    }

    [Fact]
    public async Task TheStatusDoesNotCreditAnApplicationsOwnWrapper()
    {
        var run = await Run("own_otel");
        var status = run.Summary.GetProperty("integrations");
        Assert.StartsWith("manual|", status.GetProperty("extensions_ai").GetString());
    }

    [Fact]
    public async Task AnAppThatBuildsItsOwnPipelineIsNotWrapped()
    {
        var run = await Run("direct");
        Assert.Equal(0, Count(run.Exported, ChatModel));
        Assert.StartsWith("manual|manual:", run.Summary.GetProperty("integrations").GetProperty("extensions_ai").GetString());
    }

    private sealed class Collector : IAsyncDisposable
    {
        private readonly HttpListener _listener = new();
        private readonly ConcurrentQueue<byte[]> _bodies = new();
        private readonly Task _loop;
        public string Url { get; }

        public Collector()
        {
            using var probe = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
            probe.Start();
            var port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            Url = $"http://127.0.0.1:{port}/";
            _listener.Prefixes.Add(Url);
            _listener.Start();
            _loop = Task.Run(async () =>
            {
                while (_listener.IsListening)
                {
                    HttpListenerContext context;
                    try { context = await _listener.GetContextAsync(); } catch { return; }
                    using var buffer = new MemoryStream();
                    await context.Request.InputStream.CopyToAsync(buffer);
                    _bodies.Enqueue(buffer.ToArray());
                    context.Response.StatusCode = 200;
                    context.Response.ContentType = "application/x-protobuf";
                    context.Response.Close();
                }
            });
        }

        // Latin-1 keeps every byte, so ASCII span names and attribute text can be searched in the protobuf payloads.
        public string Payload() => string.Concat(_bodies.Select(Encoding.Latin1.GetString));

        public async ValueTask DisposeAsync()
        {
            _listener.Stop();
            await _loop;
        }
    }
}
