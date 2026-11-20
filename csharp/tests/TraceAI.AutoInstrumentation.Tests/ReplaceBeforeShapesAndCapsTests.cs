using System.Collections;
using System.Diagnostics;
using System.Reflection;
using System.Text;
using OpenTelemetry;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>A known secret is replaced as a whole before any value-shape pattern or length cap can cut it: no part of it is
/// exported on either copy of a wire span (the application's copy and the Future AGI copy).</summary>
public class ReplaceBeforeShapesAndCapsTests : InitIsolation
{
    private const string Key = "synthetic-api-key-0001";
    private const string SecretKey = "synthetic-secret-key-0001";
    private static string AbsentConfig => Path.Combine(WireFixtureTests.Repo, "csharp/absent.toml");

    private sealed record Copies(string Application, string FutureAgi, Activity ApplicationSpan);

    private static string Everything(Activity span)
    {
        var text = new StringBuilder(span.DisplayName).Append('|').Append(span.StatusDescription);
        foreach (var tag in span.TagObjects) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value is Array array ? string.Join(",", array.Cast<object?>()) : tag.Value);
        foreach (var item in span.Events) { text.Append('|').Append(item.Name); foreach (var tag in item.Tags) text.Append('|').Append(tag.Key).Append('=').Append(tag.Value); }
        return text.ToString();
    }

    private static async Task<Copies> Run(Func<MockServer, Task>? beforeCall, string reply, int status, string contentType, string body, Dictionary<string, string?>? environment = null, string[]? registered = null, string? user = null, Action? afterInit = null)
    {
        var settings = new Dictionary<string, string?> { ["FI_API_KEY"] = Key, ["FI_SECRET_KEY"] = SecretKey, ["OTEL_SDK_DISABLED"] = null, ["FI_HIDE_INPUTS"] = null, ["FI_HIDE_OUTPUTS"] = null };
        foreach (var pair in environment ?? []) settings[pair.Key] = pair.Value;
        var saved = settings.Keys.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            foreach (var pair in settings) Environment.SetEnvironmentVariable(pair.Key, pair.Value);
            var hostExporter = new MemoryExporter(); var agentExporter = new MemoryExporter();
            using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
            TraceAIAuto.Init(options => { options.ConfigFile = AbsentConfig; options.Provider = provider; options.Exporter = agentExporter; });
            foreach (var secret in registered ?? []) TraceAIAuto.RegisterSecret(secret);
            afterInit?.Invoke();
            await using var server = new MockServer(Encoding.UTF8.GetBytes(reply), contentType) { StatusCode = status };
            using var client = new HttpClient();
            using var scope = user == null ? null : FIInstrumentation.Context.ContextAttributes.UsingUser(user);
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent(body, Encoding.UTF8, "application/json"))) { }
            Assert.True(TraceAIAuto.Flush());
            var application = Assert.Single(hostExporter.Spans);
            return new Copies(Everything(application), Everything(Assert.Single(agentExporter.Spans)), application);
        }
        finally { foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value); }
    }

    private static string Chat(string content, string model = "m") => "{\"model\":\"" + model + "\",\"messages\":[{\"role\":\"user\",\"content\":\"" + content + "\"}]}";

    // A known secret with a credential value shape inside it and characters outside the shape.
    private const string AwsPair = "AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
    private const string BearerSecret = "Bearer abcdefgh12345678~suffix.part-zz9";

    private static void AssertNoFragment(Copies copies, params string[] fragments)
    {
        foreach (var (name, text) in new[] { ("application", copies.Application), ("future agi", copies.FutureAgi) })
            foreach (var fragment in fragments) Assert.False(text.Contains(fragment), name + " copy holds " + fragment + ": " + text);
    }

    [Fact]
    public async Task ASecretSeededFromACredentialEnvironmentVariableLeavesNoFragmentWhenItContainsAnAwsShape()
    {
        var copies = await Run(null, "{\"error\":{\"message\":\"bad " + AwsPair + "\"}}", 401, "application/json", Chat("remember " + AwsPair, "m-" + AwsPair),
            new() { ["SYNTH_CREDENTIAL_PAIR"] = AwsPair }, user: "user-" + AwsPair);
        AssertNoFragment(copies, "wJalr", "EXAMPLEKEY", "AKIAIOSFODNN7EXAMPLE", "MDENG", ":wJ");
        Assert.Contains("remember", copies.Application); Assert.Contains("remember", copies.FutureAgi);
    }

    [Fact]
    public async Task ARegisteredSecretWithABearerShapeAndASuffixLeavesNoFragmentOnEitherCopy()
    {
        var copies = await Run(null, "{\"error\":{\"message\":\"bad " + BearerSecret + "\"}}", 401, "application/json", Chat("remember " + BearerSecret, "m-" + BearerSecret),
            registered: [BearerSecret], user: "user-" + BearerSecret);
        AssertNoFragment(copies, "abcdefgh", "12345678", "~suffix", "part-zz9");
        Assert.Contains("remember", copies.Application);
    }

    [Fact]
    public void TheRawPassNeverRunsAValueShapeBeforeKnownSecrets()
    {
        // A shapes-off scrub leaves the text as it is; the replacing scrub then removes the secret whole, shapes after secrets.
        var raw = (string)WirePrivacy.Scrub("x " + AwsPair + " y", null, shapes: false);
        Assert.Contains(AwsPair, raw);
        var replaced = (string)WirePrivacy.Scrub(raw, [AwsPair]);
        Assert.Equal("x <redacted> y", replaced);
        // A shape that is not part of a known secret is still replaced in the same call.
        Assert.Equal("<redacted> and <redacted>", (string)WirePrivacy.Scrub(AwsPair + " and AKIAABCDEFGHIJKLMNOP", [AwsPair]));
    }

    [Fact]
    public async Task ASecretWithAShapeLearnedAfterCaptureLeavesNoFragmentOnTheFutureAgiCopy()
    {
        var copies = await RunLearningLater(AwsPair, "{\"error\":{\"message\":\"bad " + AwsPair + "\"}}", 401, "application/json");
        Assert.DoesNotContain("wJalr", copies.FutureAgi); Assert.DoesNotContain("EXAMPLEKEY", copies.FutureAgi); Assert.DoesNotContain("AKIAIOSFODNN7EXAMPLE", copies.FutureAgi);
    }

    [Fact]
    public async Task FiKeysStayReplacedOnBothCopiesAfterMoreThanTheRegistryCapacityOfOtherSecrets()
    {
        var others = Enumerable.Range(0, 1100).Select(i => "filler-secret-value-" + i.ToString("D5")).ToArray();
        var copies = await Run(null, "{\"error\":{\"message\":\"bad " + SecretKey + " and " + Key + "\"}}", 401, "application/json", Chat("remember " + SecretKey + " " + Key),
            registered: others, user: "user-" + SecretKey);
        AssertNoFragment(copies, SecretKey, Key, "0001");
        Assert.Contains("remember", copies.Application);
    }

    [Theory]
    [InlineData(490)]
    [InlineData(497)]
    [InlineData(480)]
    public async Task ASecretStraddlingTheStatusDescriptionCapLeavesNoFragmentOnEitherCopy(int before)
    {
        const string secret = "STRADDLE-CAP-SECRET-5521";
        var copies = await Run(null, new string('x', before) + secret + " tail", 500, "text/plain", Chat("hello"), registered: [secret]);
        AssertNoFragment(copies, "STRADDLE", "CAP-SEC", "5521");
    }

    [Fact]
    public async Task ASecretLearnedAfterCaptureStraddlingTheStatusDescriptionCapLeavesNoFragmentOnTheFutureAgiCopy()
    {
        const string secret = "STRADDLE-LATE-SECRET-7731";
        var copies = await RunLearningLater(secret, new string('x', 492) + secret + " tail");
        Assert.DoesNotContain("STRADDLE", copies.FutureAgi); Assert.DoesNotContain("LATE-SEC", copies.FutureAgi);
    }

    private static async Task<Copies> RunLearningLater(string secret, string reply, int status = 500, string contentType = "text/plain")
    {
        var saved = new[] { "FI_API_KEY", "FI_SECRET_KEY", "OTEL_SDK_DISABLED" }.Select(name => (name, Environment.GetEnvironmentVariable(name))).ToArray();
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY", Key); Environment.SetEnvironmentVariable("FI_SECRET_KEY", SecretKey); Environment.SetEnvironmentVariable("OTEL_SDK_DISABLED", null);
            var hostExporter = new MemoryExporter(); var agentExporter = new MemoryExporter();
            using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).AddProcessor(new SimpleActivityExportProcessor(hostExporter)).Build();
            TraceAIAuto.Init(options => { options.ConfigFile = AbsentConfig; options.Provider = provider; options.Exporter = agentExporter; });
            await using var server = new MockServer(Encoding.UTF8.GetBytes(reply), contentType) { StatusCode = status };
            using var client = new HttpClient();
            using (await client.PostAsync(server.Url + "v1/chat/completions", new StringContent(Chat("hello"), Encoding.UTF8, "application/json"))) { }
            TraceAIAuto.RegisterSecret(secret);
            Assert.True(TraceAIAuto.Flush());
            var application = Assert.Single(hostExporter.Spans);
            return new Copies(Everything(application), Everything(Assert.Single(agentExporter.Spans)), application);
        }
        finally { foreach (var (name, value) in saved) Environment.SetEnvironmentVariable(name, value); }
    }

    [Theory]
    [InlineData(65536 - 10)]
    [InlineData(65536 - 3)]
    [InlineData(65536 - 20)]
    public void ASecretStraddlingTheMultipartFieldCapLeavesNoFragmentInTheCapturedField(int before)
    {
        const string secret = "STRADDLE-FIELD-SECRET-3319";
        foreach (var mode in new[] { "buffered", "observed" })
        {
            using var content = new MultipartFormDataContent("traceai-cap") { { new StringContent("whisper-1"), "model" }, { new StringContent(new string('a', before) + secret + " tail text"), "prompt" } };
            var request = new RequestData();
            if (mode == "buffered") HttpCapture.ReadRequest(content, request, 4194304);
            else
            {
                var observer = MultipartObserver.Create(content, request)!;
                using var sent = new MemoryStream(); content.CopyToAsync(sent).GetAwaiter().GetResult(); var bytes = sent.ToArray();
                for (int i = 0; i < bytes.Length; i += 4096) observer.Bytes(bytes.AsSpan(i, Math.Min(4096, bytes.Length - i)));
            }
            var field = request.Fields!["prompt"];
            Assert.False(field.Contains("STRADDLE"), mode + ": " + field[^40..]);
            Assert.True(request.Truncated);
            Assert.True(field.Length > 0);
        }
    }

    [Fact]
    public async Task NoUnreplacedSecretIsReachableFromTheApplicationsActivityAfterItEnds()
    {
        const string secret = "REACHABLE-SECRET-VALUE-4417";
        var copies = await Run(null, "{\"error\":{\"message\":\"bad " + secret + "\"}}", 401, "application/json", Chat("remember " + secret, "m-" + secret), registered: [secret], user: "user-" + secret);
        Assert.DoesNotContain("REACHABLE-SECRET", copies.Application);
        var found = Reach(copies.ApplicationSpan).Where(text => text.Contains("REACHABLE-SECRET")).ToArray();
        Assert.True(found.Length == 0, "unreplaced text reachable from the Activity: " + string.Join(" | ", found.Select(f => f[..Math.Min(60, f.Length)])));
    }

    /// <summary>Every string reachable through the Activity's own fields, custom properties, tags, events and links.</summary>
    private static IEnumerable<string> Reach(object root)
    {
        var seen = new HashSet<object>(ReferenceEqualityComparer.Instance); var queue = new Queue<(object Item, int Depth)>(); queue.Enqueue((root, 0)); var strings = new List<string>();
        while (queue.Count > 0)
        {
            var (item, depth) = queue.Dequeue();
            if (item is string text) { strings.Add(text); continue; }
            var type = item.GetType();
            if (type.IsPrimitive || type.IsEnum || depth > 8 || !seen.Add(item)) continue;
            var ns = type.Namespace ?? "";
            if (item is not IEnumerable && !(ns.StartsWith("System.Diagnostics") || ns.StartsWith("TraceAI") || ns.StartsWith("System.Collections") || ns.StartsWith("System.Runtime.CompilerServices"))) continue;
            if (item is Delegate || item is Type || item is Activity { } a && a != root && depth > 0 && ReferenceEquals(a, null)) continue;
            if (item is ActivitySource || item is Assembly || item is Thread) continue;
            if (item is IDictionary dictionary) { foreach (DictionaryEntry entry in dictionary) { if (entry.Key != null) queue.Enqueue((entry.Key, depth + 1)); if (entry.Value != null) queue.Enqueue((entry.Value, depth + 1)); } }
            else if (item is IEnumerable sequence && item is not string) { foreach (var element in sequence) if (element != null) queue.Enqueue((element, depth + 1)); }
            if (type.FullName!.StartsWith("System.Runtime.CompilerServices.ConditionalWeakTable")) continue;
            for (var t = type; t != null && t != typeof(object); t = t.BaseType)
                foreach (var field in t.GetFields(BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.DeclaredOnly))
                {
                    if (field.FieldType.IsPrimitive || field.FieldType.IsPointer) continue;
                    object? value; try { value = field.GetValue(item); } catch { continue; }
                    if (value != null && !(value is Activity)) queue.Enqueue((value, depth + 1));
                }
        }
        return strings;
    }

    [Fact]
    public async Task AnUnavailableRegistryHidesContextValuesAndContentOnBothCopies()
    {
        var copies = await Run(null, "{\"choices\":[{\"message\":{\"content\":\"reply-text-visible\"}}]}", 200, "application/json", Chat("prompt-text-visible"), user: "context-marker-user",
            afterInit: () =>
            {
                var state = (object[])typeof(KnownSecrets).GetField("State", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
                lock (state[0]) state[4] = true;
            });
        foreach (var text in new[] { copies.Application, copies.FutureAgi })
        {
            Assert.DoesNotContain("prompt-text-visible", text); Assert.DoesNotContain("reply-text-visible", text); Assert.DoesNotContain("context-marker-user", text);
        }
    }
}
