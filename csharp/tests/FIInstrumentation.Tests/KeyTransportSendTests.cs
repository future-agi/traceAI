using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;

namespace FIInstrumentation.Tests;

/// <summary>
/// What a key-carrying send does on the wire, through TraceAI.Register against local listeners: on the target as written and the host the transport
/// connects to: loopback may be plaintext; a Future AGI host is TLS only; an ambiguous host is refused; any other host in plaintext needs
/// FI_ALLOW_INSECURE_HTTP=1 and then goes direct, never through a proxy; no key-carrying request follows a redirect.
/// </summary>
public class KeyTransportSendTests : IDisposable
{
    private readonly string?[] _env = new[] { "FI_ALLOW_INSECURE_HTTP", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS" }
        .Select(Environment.GetEnvironmentVariable).ToArray();
    private static readonly string[] Names = { "FI_ALLOW_INSECURE_HTTP", "FI_API_KEY", "FI_SECRET_KEY", "FI_BASE_URL", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS" };
    private readonly IWebProxy _proxy = HttpClient.DefaultProxy;
    private readonly TextWriter _error = Console.Error;

    public KeyTransportSendTests() { TraceAI.Shutdown(); foreach (var n in Names) Environment.SetEnvironmentVariable(n, null); ResetWarning(); }
    public void Dispose()
    {
        TraceAI.Shutdown();
        for (var i = 0; i < Names.Length; i++) Environment.SetEnvironmentVariable(Names[i], _env[i]);
        HttpClient.DefaultProxy = _proxy; Console.SetError(_error); ResetWarning();
    }
    private static void ResetWarning() => typeof(TraceAI).Assembly.GetType("FIInstrumentation.FiKeyTransport")?.GetField("s_warned", BindingFlags.Static | BindingFlags.NonPublic)?.SetValue(null, 0);

    /// <summary>A loopback HTTP listener that records what it receives, optionally answering with a redirect.</summary>
    private sealed class Sink : IDisposable
    {
        private readonly HttpListener _listener = new();
        public readonly int Port;
        public readonly List<(string Method, string Path, string? ApiKey)> Requests = new();
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
                    lock (Requests) Requests.Add((c.Request.HttpMethod, c.Request.Url!.AbsolutePath, c.Request.Headers["X-Api-Key"]));
                    if (redirectTo != null) { c.Response.StatusCode = 302; c.Response.Headers["Location"] = redirectTo; } else c.Response.StatusCode = 200;
                    c.Response.Close();
                }
            });
        }
        public int Count { get { lock (Requests) return Requests.Count; } }
        public void Dispose() { _listener.Stop(); _listener.Close(); }
    }

    /// <summary>A TCP listener that only counts connections (it stands in for a proxy).</summary>
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

    private static void RegisterAndSend(string endpoint, bool grpc = false)
    {
        var tracer = TraceAI.Register(o =>
        {
            o.ProjectName = "key-transport"; o.Verbose = false; o.Batch = false; o.SetGlobalTracerProvider = false;
            if (grpc) o.Transport = FIInstrumentation.Types.Transport.Grpc;
            o.Endpoint = endpoint; o.ApiKey = "synthetic-fi-key-0001"; o.SecretKey = "synthetic-fi-secret-0001";
        });
        using (var span = tracer.ActivitySource.StartActivity("one")) { span?.SetTag("k", "v"); }
        TraceAI.Shutdown();
    }

    [Fact]
    public void LiteralLoopbackPlaintextCarriesKeysWithNoOptIn()
    {
        using var sink = new Sink();
        RegisterAndSend($"http://127.0.0.1:{sink.Port}");
        Assert.Equal("synthetic-fi-key-0001", Assert.Single(sink.Requests).ApiKey);
    }

    [Theory]
    [InlineData("http://api.futureagi.com")]
    [InlineData("http://api.futureagi.com./")]
    [InlineData("http://API.FUTUREAGI.COM:8080")]
    [InlineData("http://eu.futureagi.com")]
    public void APlaintextFutureAgiHostIsRefusedBeforeAnyRequest(string endpoint)
    {
        Assert.Equal("FI_PLAINTEXT_FI_HOST", Assert.Throws<FiKeyRefusedException>(() => RegisterAndSend(endpoint)).Code);
        Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        TraceAI.Shutdown();
        Assert.Equal("FI_PLAINTEXT_FI_HOST", Assert.Throws<FiKeyRefusedException>(() => RegisterAndSend(endpoint)).Code);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void LoopbackPlaintextIsDirectWhenAProxyIsConfigured(bool optIn)
    {
        using var sink = new Sink(); using var proxy = new Listener();
        HttpClient.DefaultProxy = new WebProxy($"http://127.0.0.1:{proxy.Port}", BypassOnLocal: false);
        if (optIn) Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        RegisterAndSend($"http://127.0.0.1:{sink.Port}");
        Assert.Equal(1, sink.Count);
        Assert.Equal(0, proxy.Connections);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void AGrpcPlaintextSendIsDirectWhenAProxyIsConfigured(bool optIn)
    {
        // gRPC to a plaintext target (loopback, or the opted-in self-hosted spelling): the connection goes to the target, not the proxy.
        using var target = new Listener(); using var proxy = new Listener();
        HttpClient.DefaultProxy = new WebProxy($"http://127.0.0.1:{proxy.Port}", BypassOnLocal: false);
        if (optIn) Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        var endpoint = optIn ? $"http://127.1:{target.Port}" : $"http://127.0.0.1:{target.Port}";
        try { RegisterAndSend(endpoint, grpc: true); } catch (FiKeyRefusedException) { throw; } catch { /* the listener never speaks gRPC; a failed export is not the point */ }
        Assert.True(target.Connections >= 1, "the gRPC connection went to the target");
        Assert.Equal(0, proxy.Connections);
    }

    [Theory]
    [InlineData("http://api.futureagi.com\\@collector.example", "FI_HOST_MISMATCH")]
    [InlineData("http://collector.example%5C@api.futureagi.com", "FI_HOST_MISMATCH")]
    [InlineData("http://collector.example/x@api.futureagi.com", "FI_HOST_MISMATCH")]
    [InlineData("http://localhost\\@collector.example:4318", "FI_HOST_MISMATCH")]
    [InlineData("http:///api.futureagi.com", "FI_EMPTY_AUTHORITY")]
    public void AnAmbiguousTargetIsRefused(string endpoint, string code)
    {
        Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        Assert.Equal(code, Assert.Throws<FiKeyRefusedException>(() => RegisterAndSend(endpoint)).Code);
    }

    [Fact]
    public void ASelfHostedPlaintextTargetIsRefusedUnlessTheOptInIsSet()
    {
        // 127.1 is not the literal loopback (a parser reads it as 127.0.0.1): it needs the opt-in, and still reaches this machine.
        using var sink = new Sink();
        var endpoint = $"http://127.1:{sink.Port}";
        Assert.Equal("FI_PLAINTEXT_NOT_ALLOWED", Assert.Throws<FiKeyRefusedException>(() => RegisterAndSend(endpoint)).Code);
        Assert.Equal(0, sink.Count);
        TraceAI.Shutdown();
        Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "yes");
        Assert.Equal("FI_PLAINTEXT_NOT_ALLOWED", Assert.Throws<FiKeyRefusedException>(() => RegisterAndSend(endpoint)).Code);
        Assert.Equal(0, sink.Count);
    }

    [Fact]
    public void TheOptInSendsInPlaintextWithOneWarningThatNamesTheSettingAndTheOrigin()
    {
        using var sink = new Sink();
        var errors = new StringWriter(); Console.SetError(errors);
        Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        RegisterAndSend($"http://127.1:{sink.Port}");
        RegisterAndSend($"http://127.1:{sink.Port}");
        Assert.Equal(2, sink.Count);
        var warnings = errors.ToString().Split('\n').Where(l => l.Contains("FI_ALLOW_INSECURE_HTTP")).ToArray();
        Assert.Single(warnings);
        Assert.Contains($"http://127.0.0.1:{sink.Port}", warnings[0]);
        Assert.DoesNotContain("synthetic", errors.ToString());
    }

    [Fact]
    public void ThePlaintextOptInGoesDirectWhenAProxyIsConfigured()
    {
        using var sink = new Sink(); using var proxy = new Listener();
        HttpClient.DefaultProxy = new WebProxy($"http://127.0.0.1:{proxy.Port}", BypassOnLocal: false);
        Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        RegisterAndSend($"http://127.1:{sink.Port}");
        Assert.Equal(1, sink.Count);
        Assert.Equal(0, proxy.Connections);
    }

    [Fact]
    public void AnHttpsSendKeepsTheNormalProxyBehaviour()
    {
        using var proxy = new Listener();
        HttpClient.DefaultProxy = new WebProxy($"http://127.0.0.1:{proxy.Port}", BypassOnLocal: false);
        RegisterAndSend("https://collector.example:4318");
        Assert.True(proxy.Connections >= 1, "the https request goes to the proxy (a CONNECT tunnel)");
        lock (proxy.FirstLines) Assert.Contains(proxy.FirstLines, l => l.StartsWith("CONNECT collector.example:4318"));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ARedirectIsNotFollowedAndNothingGoesToItsTarget(bool plaintextOptIn)
    {
        using var target = new Sink();
        using var first = new Sink(redirectTo: $"http://127.0.0.1:{target.Port}/redirected");
        if (plaintextOptIn) Environment.SetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP", "1");
        RegisterAndSend(plaintextOptIn ? $"http://127.1:{first.Port}" : $"http://127.0.0.1:{first.Port}");
        Assert.Equal(1, first.Count);
        Assert.Equal(0, target.Count);
    }
}
