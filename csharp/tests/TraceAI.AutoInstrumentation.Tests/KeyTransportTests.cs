using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Trace;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>
/// The Future AGI route the agent builds is decided in one place (FiKeyTransport.Decide) before any key header is attached: loopback may be
/// plaintext; a Future AGI host is TLS only; an ambiguous host is refused; another host in plaintext needs FI_ALLOW_INSECURE_HTTP=1 and then
/// goes direct, never through a proxy; no key-carrying request follows a redirect (either transport the agent uses).
/// </summary>
public class KeyTransportAutoTests : InitIsolation
{
    private static readonly string[] Names = { "FI_ALLOW_INSECURE_HTTP", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS" };
    private readonly string?[] _env = Names.Select(Environment.GetEnvironmentVariable).ToArray();
    private readonly IWebProxy _proxy = HttpClient.DefaultProxy;
    private readonly TextWriter _error = Console.Error;
    public KeyTransportAutoTests() { foreach (var n in Names) Environment.SetEnvironmentVariable(n, null); ResetWarning(); }
    private void Restore()
    {
        for (var i = 0; i < Names.Length; i++) Environment.SetEnvironmentVariable(Names[i], _env[i]);
        HttpClient.DefaultProxy = _proxy; Console.SetError(_error); ResetWarning();
    }
    private static void ResetWarning() => typeof(FiKeyTransport).GetField("s_warned", BindingFlags.Static | BindingFlags.NonPublic)!.SetValue(null, 0);
    private static AutoConfig Config(string? baseUrl, bool? allow = null, BaseExporter<Activity>? exporter = null) => new(new AutoOptions {
        ApiKey = "synthetic-fi-key-0001", SecretKey = "synthetic-fi-secret-0001", BaseUrl = baseUrl, AllowInsecureHttp = allow, Exporter = exporter,
        ConfigFile = Path.Combine(WireFixtureTests.Repo, "csharp", "absent.toml") });

    private sealed class Sink : IDisposable
    {
        private readonly HttpListener _listener = new();
        public readonly int Port;
        public int Count; public string? FirstApiKey;
        public Sink(string? redirectTo = null)
        {
            using var probe = new TcpListener(IPAddress.Loopback, 0); probe.Start(); Port = ((IPEndPoint)probe.LocalEndpoint).Port; probe.Stop();
            _listener.Prefixes.Add($"http://127.0.0.1:{Port}/"); _listener.Start();
            _ = Task.Run(async () =>
            {
                while (_listener.IsListening)
                {
                    HttpListenerContext c;
                    try { c = await _listener.GetContextAsync(); } catch { return; }
                    using (var drain = new MemoryStream()) await c.Request.InputStream.CopyToAsync(drain);
                    if (Interlocked.Increment(ref Count) == 1) FirstApiKey = c.Request.Headers["X-Api-Key"];
                    if (redirectTo != null) { c.Response.StatusCode = 302; c.Response.Headers["Location"] = redirectTo; } else c.Response.StatusCode = 200;
                    c.Response.Close();
                }
            });
        }
        public void Dispose() { _listener.Stop(); _listener.Close(); }
    }
    private sealed class Listener : IDisposable
    {
        private readonly TcpListener _tcp = new(IPAddress.Loopback, 0);
        public int Connections; public readonly List<string> FirstLines = new();
        public int Port => ((IPEndPoint)_tcp.LocalEndpoint).Port;
        public Listener()
        {
            _tcp.Start();
            _ = Task.Run(async () =>
            {
                while (true)
                {
                    TcpClient client;
                    try { client = await _tcp.AcceptTcpClientAsync(); } catch { return; }
                    Interlocked.Increment(ref Connections);
                    try { var buffer = new byte[512]; var n = await client.GetStream().ReadAsync(buffer); lock (FirstLines) FirstLines.Add(Encoding.ASCII.GetString(buffer, 0, n).Split('\r')[0]); } catch { }
                    client.Dispose();
                }
            });
        }
        public void Dispose() => _tcp.Stop();
    }

    /// <summary>One span through the route the agent builds, on the transport the agent selects (the native SDK or the compatibility encoder).</summary>
    private static void Send(AutoConfig config, bool compatibility)
    {
        var privacy = new AutoProcessor(config);
        var options = Agent.FiRouteOptions(config);
        using var provider = Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name).AddProcessor(privacy)
            .AddProcessor(new SimpleActivityExportProcessor(new TrackingOtlpExporter(options, privacy, new ExportCompletion(), compatibility))).Build();
        using (TraceAIAuto.Source.StartActivity("one", ActivityKind.Client, default(ActivityContext), new ActivityTagsCollection { ["traceai.capture.layer"] = "wire" })) { }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void LiteralLoopbackPlaintextCarriesKeysWithNoOptIn(bool compatibility)
    {
        try
        {
            using var sink = new Sink();
            Send(Config($"http://127.0.0.1:{sink.Port}"), compatibility);
            Assert.Equal(1, sink.Count); Assert.Equal("synthetic-fi-key-0001", sink.FirstApiKey);
        }
        finally { Restore(); }
    }

    [Theory]
    [InlineData("http://api.futureagi.com")]
    [InlineData("http://api.futureagi.com./")]
    [InlineData("http://EU.FutureAGI.com:8080")]
    public void APlaintextFutureAgiHostIsRefusedWithAndWithoutTheOptIn(string baseUrl)
    {
        try
        {
            Assert.ThrowsAny<Exception>(() => Agent.FiRouteOptions(Config(baseUrl)));
            Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
            Assert.ThrowsAny<Exception>(() => Agent.FiRouteOptions(Config(baseUrl)));
            Assert.ThrowsAny<Exception>(() => Agent.FiRouteOptions(Config(baseUrl, allow: true)));
        }
        finally { Restore(); }
    }

    [Theory]
    [InlineData("http://api.futureagi.com\\@collector.example")]
    [InlineData("http://collector.example%5C@api.futureagi.com")]
    [InlineData("http://collector.example/x@api.futureagi.com")]
    [InlineData("http://localhost\\@collector.example:4318")]
    [InlineData("http:///api.futureagi.com")]
    public void AnAmbiguousBaseUrlBuildsNoRoute(string baseUrl)
    {
        try
        {
            Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
            Assert.ThrowsAny<Exception>(() => Agent.FiRouteOptions(Config(baseUrl, allow: true)));
        }
        finally { Restore(); }
    }

    [Fact]
    public void ASelfHostedPlaintextBaseUrlIsRefusedUnlessTheOptInIsSet()
    {
        try
        {
            Assert.Equal("FI_PLAINTEXT_NOT_ALLOWED", Assert.Throws<FiKeyRefusedException>(() => Agent.FiRouteOptions(Config("http://collector.example:4318"))).Code);
            Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "true");
            Assert.Throws<FiKeyRefusedException>(() => Agent.FiRouteOptions(Config("http://collector.example:4318")));
            var errors = new StringWriter(); Console.SetError(errors);
            Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
            Agent.FiRouteOptions(Config("http://collector.example:4318"));
            Agent.FiRouteOptions(Config("http://other.example:4318"));
            var warnings = errors.ToString().Split('\n').Where(l => l.Contains("FI_ALLOW_INSECURE_HTTP")).ToArray();
            Assert.Single(warnings); Assert.Contains("http://collector.example:4318", warnings[0]); Assert.DoesNotContain("synthetic", errors.ToString());
        }
        finally { Restore(); }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ThePlaintextOptInGoesDirectWhenAProxyIsConfigured(bool compatibility)
    {
        try
        {
            using var sink = new Sink(); using var proxy = new Listener();
            HttpClient.DefaultProxy = new WebProxy($"http://127.0.0.1:{proxy.Port}", BypassOnLocal: false);
            Send(Config($"http://127.1:{sink.Port}", allow: true), compatibility);
            Assert.Equal(1, sink.Count); Assert.Equal(0, proxy.Connections);
        }
        finally { Restore(); }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void AnHttpsRouteKeepsTheNormalProxyBehaviour(bool compatibility)
    {
        try
        {
            using var proxy = new Listener();
            HttpClient.DefaultProxy = new WebProxy($"http://127.0.0.1:{proxy.Port}", BypassOnLocal: false);
            Send(Config("https://collector.example:4318"), compatibility);
            Assert.True(proxy.Connections >= 1);
            lock (proxy.FirstLines) Assert.Contains(proxy.FirstLines, l => l.StartsWith("CONNECT collector.example:4318"));
        }
        finally { Restore(); }
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, false)]
    [InlineData(false, true)]
    [InlineData(true, true)]
    public void ARedirectIsNotFollowedAndNothingGoesToItsTarget(bool compatibility, bool plaintextOptIn)
    {
        try
        {
            using var target = new Sink();
            using var first = new Sink(redirectTo: $"http://127.0.0.1:{target.Port}/redirected");
            Send(plaintextOptIn ? Config($"http://127.1:{first.Port}", allow: true) : Config($"http://127.0.0.1:{first.Port}"), compatibility);
            Assert.Equal(1, first.Count); Assert.Equal(0, target.Count);
        }
        finally { Restore(); }
    }

    private sealed class NullExporter : BaseExporter<Activity> { public override ExportResult Export(in Batch<Activity> batch) => ExportResult.Success; }

    [Theory]
    [InlineData("x-api-key=ambient-key-synthetic-0001")]
    [InlineData("X-Secret-Key=ambient-secret-synthetic-0001,authorization=Bearer%20x")]
    public void AnExporterTheCallerSuppliesIsRefusedWhenTheAmbientHeadersCarryAKeyHeader(string headers)
    {
        try
        {
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", headers);
            Assert.ThrowsAny<Exception>(() => new Agent(Config(null, exporter: new NullExporter())));
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", null);
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_HEADERS", headers);
            Assert.ThrowsAny<Exception>(() => new Agent(Config(null, exporter: new NullExporter())));
        }
        finally { Restore(); }
    }

    [Fact]
    public void AKnownSecretInAnAmbientHeaderRefusesTheSuppliedExporterAndAnUnrelatedHeaderDoesNot()
    {
        try
        {
            TraceAIAuto.RegisterSecret("registered-secret-synthetic-0004");
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=registered-secret-synthetic-0004");
            Assert.ThrowsAny<Exception>(() => new Agent(Config(null, exporter: new NullExporter())));
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer%20other-backend-token");
            using var agent = new Agent(Config(null, exporter: new NullExporter()));
        }
        finally { Restore(); }
    }

    [Fact]
    public void InitReportsARefusedRouteWithTheSettingToChangeAndNoValue()
    {
        try
        {
            var errors = new StringWriter(); Console.SetError(errors);
            TraceAIAuto.Init(o => { o.ApiKey = "synthetic-fi-key-0001"; o.SecretKey = "synthetic-fi-secret-0001"; o.BaseUrl = "http://collector.example:4318"; o.ConfigFile = Path.Combine(WireFixtureTests.Repo, "csharp", "absent.toml"); });
            var text = errors.ToString();
            Assert.Contains("FI_ALLOW_INSECURE_HTTP", text);
            Assert.DoesNotContain("synthetic", text); Assert.DoesNotContain("collector.example", text);
        }
        finally { Restore(); }
    }
}
