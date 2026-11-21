using System.Diagnostics;
using System.Reflection;
using System.Text;
using OpenTelemetry;
using OpenTelemetry.Trace;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>Overlapping known secrets leave no fragment on either copy of a wire span: the application's copy (replaced when
/// the span ends) and the Future AGI copy (replaced at export, from the unreplaced values and the registry at that moment).</summary>
public class OverlappingKnownSecretsTests : InitIsolation
{
    private static string AbsentConfig => Path.Combine(WireFixtureTests.Repo, "csharp/absent.toml");

    private sealed record Copies(string Application, string FutureAgi);

    private static string Everything(Activity span)
    {
        var text = new StringBuilder(span.DisplayName).Append('|').Append(span.StatusDescription);
        foreach (var tag in span.TagObjects) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value is Array array ? string.Join(",", array.Cast<object?>()) : tag.Value);
        foreach (var item in span.Events) { text.Append('|').Append(item.Name); foreach (var tag in item.Tags) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value); }
        return text.ToString();
    }

    /// <summary>One model call whose request body, model name, error reply and request context all carry <paramref name="value"/>.</summary>
    private static async Task<Copies> Capture(string[] knownBefore, string value, string[] learnedAfterCapture, Action<Dictionary<string, string?>>? environment = null, string? requestBody = null)
    {
        var settings = new Dictionary<string, string?> { ["FI_API_KEY"] = "synthetic-api-key-0001", ["FI_SECRET_KEY"] = "synthetic-secret-key-0001", ["OTEL_SDK_DISABLED"] = null, ["FI_HIDE_INPUTS"] = null, ["FI_HIDE_OUTPUTS"] = null };
        environment?.Invoke(settings);
        var saved = settings.Keys.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            foreach (var pair in settings) Environment.SetEnvironmentVariable(pair.Key, pair.Value);
            var hostExporter = new MemoryExporter(); var agentExporter = new MemoryExporter();
            using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
            TraceAIAuto.Init(options => { options.ConfigFile = AbsentConfig; options.Provider = provider; options.Exporter = agentExporter; });
            foreach (var secret in knownBefore) TraceAIAuto.RegisterSecret(secret);
            await using var server = new MockServer(Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"bad " + value + "\"}}"), "application/json") { StatusCode = 401 };
            using var client = new HttpClient();
            using var scope = FIInstrumentation.Context.ContextAttributes.UsingUser("user-" + value);
            var body = requestBody ?? "{\"model\":\"m-" + value + "\",\"messages\":[{\"role\":\"user\",\"content\":\"remember " + value + "\"}]}";
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent(body, Encoding.UTF8, "application/json"))) { }
            // Learned after the span was captured and before it is exported.
            foreach (var secret in learnedAfterCapture) TraceAIAuto.RegisterSecret(secret);
            Assert.True(TraceAIAuto.Flush());
            return new Copies(Everything(Assert.Single(hostExporter.Spans)), Everything(Assert.Single(agentExporter.Spans)));
        }
        finally { foreach (var (name, value2) in saved) Environment.SetEnvironmentVariable(name, value2); }
    }

    private const string Short = "PFX-SHORT-01";
    private const string Long = "PFX-SHORT-01-LONGPART-77";

    [Fact]
    public async Task ALongerSecretLearnedAfterCaptureLeavesNoFragmentOnTheFutureAgiCopy()
    {
        var copies = await Capture([Short], "pre " + Long + " post", [Long]);
        Assert.DoesNotContain("LONGPART", copies.FutureAgi); Assert.DoesNotContain(Short, copies.FutureAgi); Assert.DoesNotContain("-77", copies.FutureAgi);
        // The application's copy is replaced when the span ends, with the secrets known then.
        Assert.DoesNotContain(Short, copies.Application);
        Assert.Contains("remember", copies.FutureAgi);
    }

    [Fact]
    public async Task AShorterSecretLearnedAfterCaptureLeavesNoFragmentOnEitherCopy()
    {
        var copies = await Capture([Long], "pre " + Long + " post", [Short]);
        foreach (var text in new[] { copies.Application, copies.FutureAgi }) { Assert.DoesNotContain("LONGPART", text); Assert.DoesNotContain("SHORT", text); Assert.DoesNotContain("-77", text); }
    }

    [Fact]
    public async Task ASecretThatIsASubstringOfAnotherLeavesNoFragmentOnEitherCopy()
    {
        var copies = await Capture([Short, Long], "pre " + Long + " post", []);
        foreach (var text in new[] { copies.Application, copies.FutureAgi }) { Assert.DoesNotContain("LONGPART", text); Assert.DoesNotContain("SHORT", text); Assert.DoesNotContain("-77", text); }
    }

    [Fact]
    public async Task SecretsThatOverlapAtTheirEdgesLeaveNoFragmentOnEitherCopy()
    {
        var copies = await Capture(["AAAA-BBBB-01", "BBBB-01-CCCC"], "x AAAA-BBBB-01-CCCC y", []);
        foreach (var text in new[] { copies.Application, copies.FutureAgi }) { Assert.DoesNotContain("AAAA", text); Assert.DoesNotContain("BBBB", text); Assert.DoesNotContain("CCCC", text); }
    }

    [Fact]
    public async Task ASecretStraddlingTheSdkValueLengthLimitIsReplacedBeforeTheLimitCutsIt()
    {
        // The exporter's attribute value length limit runs after the agent's replacement, so a cut inside a secret leaves no fragment.
        const string secret = "STRADDLE-SECRET-9911";
        var saved = new[] { "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "FI_CONFIG_FILE", "OTEL_SDK_DISABLED", "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT" }.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            await using var server = new MockServer("{\"choices\":[{\"message\":{\"content\":\"ok\"}}]}"u8.ToArray(), "application/json");
            Environment.SetEnvironmentVariable("FI_API_KEY", "synthetic-api-key-0001"); Environment.SetEnvironmentVariable("FI_SECRET_KEY", "synthetic-secret-key-0001");
            Environment.SetEnvironmentVariable("FI_BASE_URL", server.Url); Environment.SetEnvironmentVariable("FI_CONFIG_FILE", AbsentConfig); Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", null);
            Environment.SetEnvironmentVariable("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "40");
            TraceAIAuto.Init();
            TraceAIAuto.RegisterSecret(secret);
            var body = "{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"" + new string('p', 6) + secret + " tail text\"}]}";
            using var client = new HttpClient();
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent(body, Encoding.UTF8, "application/json"))) { }
            Assert.True(TraceAIAuto.Flush());
            var exported = string.Concat(server.Exports.Select(bytes => Encoding.Latin1.GetString(bytes)));
            Assert.DoesNotContain("STRADDLE", exported); Assert.DoesNotContain("SECRET-99", exported);
            Assert.Contains("pppppp", exported);
        }
        finally { foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value); }
    }

    [Fact]
    public async Task ASecretLearnedAfterCaptureIsReplacedInContextValuesAndEverySurfaceOnTheFutureAgiCopy()
    {
        var copies = await Capture([], "pre " + Long + " post", [Long]);
        Assert.DoesNotContain("PFX-SHORT", copies.FutureAgi); Assert.DoesNotContain("LONGPART", copies.FutureAgi);
        Assert.Contains("user-", copies.FutureAgi);
    }

    [Fact]
    public async Task AnUnavailableRegistryReplacesTheContentOfBothCopiesWithTheMarker()
    {
        var saved = new[] { "FI_API_KEY", "FI_SECRET_KEY", "OTEL_SDK_DISABLED" }.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY", "synthetic-api-key-0001"); Environment.SetEnvironmentVariable("FI_SECRET_KEY", "synthetic-secret-key-0001"); Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", null);
            var hostExporter = new MemoryExporter();
            using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
            TraceAIAuto.Init(options => { options.ConfigFile = AbsentConfig; options.Provider = provider; });
            var state = (object[])typeof(KnownSecrets).GetField("State", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
            lock (state[0]) state[4] = true;
            await using var server = new MockServer("{\"choices\":[{\"message\":{\"content\":\"reply-text-visible\"}}]}"u8.ToArray(), "application/json");
            using var client = new HttpClient();
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent("{\"model\":\"m\",\"messages\":[{\"role\":\"user\",\"content\":\"prompt-text-visible\"}]}", Encoding.UTF8, "application/json"))) { }
            Assert.True(TraceAIAuto.Flush());
            var text = Everything(Assert.Single(hostExporter.Spans));
            Assert.DoesNotContain("prompt-text-visible", text); Assert.DoesNotContain("reply-text-visible", text);
        }
        finally { foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value); }
    }

    public static IEnumerable<object[]> Chains => new[]
    {
        new object[] { "one character", new[] { "AAAAAAA1", "1BBBBBBB" }, "AAAAAAA1BBBBBBB", new[] { "AAAA", "BBBB" } },
        new object[] { "two characters", new[] { "CCCCCC12", "12DDDDDD" }, "CCCCCC12DDDDDD", new[] { "CCCC", "DDDD" } },
        new object[] { "three characters", new[] { "EEEEE123", "123FFFFF" }, "EEEEE123FFFFF", new[] { "EEEE", "FFFF" } },
        new object[] { "three-secret chain", new[] { "GGGGGG-A", "A-HHHH-B", "B-IIIIII" }, "GGGGGG-A-HHHH-B-IIIIII", new[] { "GGGG", "HHHH", "IIII" } },
        new object[] { "two inner secrets in one outer", new[] { "OUT-INNERONE-mid-INNERTWO-END", "INNERONE", "INNERTWO" }, "pre OUT-INNERONE-mid-INNERTWO-END post", new[] { "INNER", "-mid-", "-END" } },
        new object[] { "four-secret chain", new[] { "WWWWWWW1", "1XXXXXX2", "2YYYYYY3", "3ZZZZZZZ" }, "WWWWWWW1XXXXXX2YYYYYY3ZZZZZZZ", new[] { "WWWW", "XXXX", "YYYY", "ZZZZ" } },
    };

    [Theory]
    [MemberData(nameof(Chains))]
    public async Task OverlappingSecretsOfEveryShapeLeaveNoFragmentOnEitherCopy(string shape, string[] secrets, string value, string[] fragments)
    {
        foreach (var order in new[] { secrets, secrets.Reverse().ToArray() })
        {
            TraceAIAuto.Shutdown(); ResetForAnotherRun();
            var copies = await Capture(order, value, []);
            foreach (var text in new[] { copies.Application, copies.FutureAgi }) foreach (var fragment in fragments) Assert.False(text.Contains(fragment), shape + ": " + fragment + " in " + text);
        }
    }

    [Theory]
    [MemberData(nameof(Chains))]
    public async Task SecretsLearnedAfterCaptureLeaveNoFragmentOnTheFutureAgiCopy(string shape, string[] secrets, string value, string[] fragments)
    {
        var copies = await Capture([], value, secrets);
        foreach (var fragment in fragments) Assert.False(copies.FutureAgi.Contains(fragment), shape + ": " + fragment + " in " + copies.FutureAgi);
    }

    private static void ResetForAnotherRun()
    {
        const BindingFlags flags = BindingFlags.Static | BindingFlags.NonPublic;
        ResetSecrets();
        typeof(TraceAIAuto).GetField("s_initialized", flags)!.SetValue(null, false);
        AppDomain.CurrentDomain.SetData("TraceAI.AutoInstrumentation.InitState", null);
        typeof(TraceAIAuto).GetField("s_config", flags)!.SetValue(null, null);
    }
}
