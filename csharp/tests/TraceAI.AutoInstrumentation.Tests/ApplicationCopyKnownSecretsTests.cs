using System.Diagnostics;
using System.Text;
using OpenTelemetry;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>The spans the agent creates are also seen by the application's own provider. A secret the agent knows is replaced
/// on those spans exactly as on the Future AGI copy, whether or not any hide setting is on.</summary>
public class ApplicationCopyKnownSecretsTests : InitIsolation
{
    private const string Secret = "synthetic-known-secret-value-4f2a9c";
    private const string Registered = "synthetic-registered-secret-value-81d3";
    private static string AbsentConfig => Path.Combine(WireFixtureTests.Repo, "csharp/absent.toml");

    private static string Everything(Activity span)
    {
        var text = new StringBuilder(span.DisplayName).Append('|').Append(span.StatusDescription);
        foreach (var tag in span.TagObjects) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value);
        foreach (var item in span.Events) { text.Append('|').Append(item.Name); foreach (var tag in item.Tags) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value); }
        foreach (var link in span.Links) { foreach (var tag in link.Tags ?? []) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value); }
        return text.ToString();
    }

    [Theory]
    [InlineData(false, 200)]
    [InlineData(true, 200)]
    [InlineData(false, 401)]
    [InlineData(true, 401)]
    public async Task AKnownSecretIsReplacedOnTheApplicationsCopyAndTheFutureAgiCopy(bool hideOutputs, int status)
    {
        var saved = new[] { "FI_API_KEY", "FI_SECRET_KEY", "FI_HIDE_OUTPUTS", "OTEL_SDK_DISABLED" }.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY", "synthetic-api-key-0001");
            Environment.SetEnvironmentVariable("FI_SECRET_KEY", Secret);
            Environment.SetEnvironmentVariable("FI_HIDE_OUTPUTS", hideOutputs ? "true" : null);
            Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", null);
            var hostExporter = new MemoryExporter(); var agentExporter = new MemoryExporter();
            using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
                .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(new Dictionary<string, object> { ["service.name"] = "checkout" }))
                .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
            TraceAIAuto.Init(options => { options.ConfigFile = AbsentConfig; options.Provider = provider; options.Exporter = agentExporter; });
            var reply = status == 200
                ? "{\"choices\":[{\"message\":{\"content\":\"your key is " + Secret + "\"}}]}"
                : "{\"error\":{\"message\":\"bad key " + Secret + "\"}}";
            await using var server = new MockServer(Encoding.UTF8.GetBytes(reply), "application/json") { StatusCode = status };
            using var client = new HttpClient();
            var request = "{\"model\":\"m-" + Secret + "\",\"messages\":[{\"role\":\"user\",\"content\":\"remember " + Secret + "\"}]}";
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent(request, Encoding.UTF8, "application/json"))) { }
            Assert.True(TraceAIAuto.Flush());
            var applicationSpan = Assert.Single(hostExporter.Spans);
            var futureAgiSpan = Assert.Single(agentExporter.Spans);
            Assert.DoesNotContain(Secret, Everything(applicationSpan));
            Assert.DoesNotContain(Secret, Everything(futureAgiSpan));
            // The input is not hidden by FI_HIDE_OUTPUTS, so the rest of it stays: only the secret is replaced.
            Assert.Contains("remember", Everything(applicationSpan));
        }
        finally { foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value); }
    }

    [Theory]
    [InlineData("user", false, false)]
    [InlineData("session", false, false)]
    [InlineData("metadata", false, false)]
    [InlineData("tags", false, false)]
    [InlineData("metadata", true, false)]
    [InlineData("user", false, true)]
    [InlineData("session", false, true)]
    [InlineData("metadata", false, true)]
    [InlineData("tags", false, true)]
    [InlineData("metadata", true, true)]
    public async Task AKnownSecretInTheRequestContextIsReplacedOnTheApplicationsCopyToo(string field, bool hideInputs, bool registered)
    {
        var known = registered ? Registered : Secret;
        var saved = new[] { "FI_API_KEY", "FI_SECRET_KEY", "FI_HIDE_INPUTS", "OTEL_SDK_DISABLED" }.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY", "synthetic-api-key-0001");
            Environment.SetEnvironmentVariable("FI_SECRET_KEY", Secret);
            Environment.SetEnvironmentVariable("FI_HIDE_INPUTS", hideInputs ? "true" : null);
            Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", null);
            var hostExporter = new MemoryExporter(); var agentExporter = new MemoryExporter();
            using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
                .AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
            TraceAIAuto.Init(options => { options.ConfigFile = AbsentConfig; options.Provider = provider; options.Exporter = agentExporter; });
            if (registered) TraceAIAuto.RegisterSecret(Registered);
            await using var server = new MockServer("{\"choices\":[{\"message\":{\"content\":\"fine\"}}]}"u8.ToArray(), "application/json");
            using var client = new HttpClient();
            using var scope = field switch
            {
                "user" => FIInstrumentation.Context.ContextAttributes.UsingUser("marker-user-" + known),
                "session" => FIInstrumentation.Context.ContextAttributes.UsingSession("marker-session-" + known),
                "metadata" => FIInstrumentation.Context.ContextAttributes.UsingMetadata(new Dictionary<string, object> { ["note"] = "marker-keep " + known }),
                _ => FIInstrumentation.Context.ContextAttributes.UsingTags(["marker-tag-" + known]),
            };
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent("{\"model\":\"m\",\"messages\":[]}", Encoding.UTF8, "application/json"))) { }
            Assert.True(TraceAIAuto.Flush());
            var applicationText = Everything(Assert.Single(hostExporter.Spans));
            Assert.DoesNotContain(known, applicationText);
            Assert.DoesNotContain(known, Everything(Assert.Single(agentExporter.Spans)));
            // Control: the context value is on the application's copy, with only the secret replaced.
            Assert.Contains("marker-", applicationText);
        }
        finally { foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value); }
    }
}
