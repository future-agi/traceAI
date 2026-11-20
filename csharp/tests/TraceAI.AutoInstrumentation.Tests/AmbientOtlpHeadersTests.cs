using System.Text;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>
/// Zero-code path audit: the real startup hook runs in a child process whose environment
/// carries synthetic ambient OTLP header variables. The capture server is loopback only.
/// </summary>
public class AmbientOtlpHeadersTests
{
    private const string Ambient = "Authorization=Bearer SYNTHETIC-OTHER-BACKEND,x-synthetic-marker=ambient-marker-1";
    private const string Reply = "{\"id\":\"local-id\",\"model\":\"local-test\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello back\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";

    [Theory]
    [InlineData("OTEL_EXPORTER_OTLP_HEADERS", "net8.0")]
    [InlineData("OTEL_EXPORTER_OTLP_HEADERS", "net10.0")]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "net8.0")]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "net10.0")]
    public async Task AmbientOtlpHeaderVariableIsNotSentFromStartupHook(string variable, string framework)
    {
        await using var server = new MockServer(Encoding.UTF8.GetBytes(Reply), "application/json");
        var result = await StartupHookTests.RunApp(server.Url + "v1/chat/completions", "json", new Dictionary<string, string?> {
            ["FI_BASE_URL"] = server.Url, ["FI_API_KEY"] = "synthetic-fi-key", ["FI_SECRET_KEY"] = "synthetic-fi-secret",
            ["FI_PROJECT_NAME"] = "hook-test", ["FI_CONFIG_FILE"] = Path.Combine(WireFixtureTests.Repo, "csharp", "no-such-config.toml"),
            [variable] = Ambient }, framework);
        Assert.Equal(0, result.ExitCode);
        var headers = Assert.Single(server.ExportHeaders);
        Assert.Equal("synthetic-fi-key", headers["X-Api-Key"]);
        Assert.Equal("synthetic-fi-secret", headers["X-Secret-Key"]);
        Assert.Equal("hook-test", headers["fi-project-name"]);
        Assert.False(headers.ContainsKey("Authorization"));
        Assert.False(headers.ContainsKey("x-synthetic-marker"));
    }
}
