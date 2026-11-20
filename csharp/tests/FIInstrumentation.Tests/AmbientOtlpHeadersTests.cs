using System.Net;
using System.Net.Sockets;
using System.Collections.Concurrent;

namespace FIInstrumentation.Tests;

/// <summary>
/// Audit of ambient OTLP header variables against the FI-bound exporter. A loopback-only
/// capture server stands in for the endpoint; every value is synthetic and the variables
/// are set only inside each test.
/// </summary>
[Collection("Sampler environment")]
public class AmbientOtlpHeadersTests : IDisposable
{
    private const string Ambient = "Authorization=Bearer SYNTHETIC-OTHER-BACKEND,x-synthetic-marker=ambient-marker-1";
    private readonly Dictionary<string, string?> _saved = new();

    public AmbientOtlpHeadersTests() => TraceAI.Shutdown();

    public void Dispose()
    {
        TraceAI.Shutdown();
        foreach (var (name, value) in _saved) Environment.SetEnvironmentVariable(name, value);
    }

    private void SetEnv(string name, string? value)
    {
        if (!_saved.ContainsKey(name)) _saved[name] = Environment.GetEnvironmentVariable(name);
        Environment.SetEnvironmentVariable(name, value);
    }

    private static async Task<Dictionary<string, string>> ExportOneSpan()
    {
        var port = new TcpListener(IPAddress.Loopback, 0); port.Start();
        int number = ((IPEndPoint)port.LocalEndpoint).Port; port.Stop();
        var url = $"http://127.0.0.1:{number}/";
        using var listener = new HttpListener();
        listener.Prefixes.Add(url); listener.Start();
        var captured = new ConcurrentQueue<Dictionary<string, string>>();
        var serve = Task.Run(async () =>
        {
            try
            {
                while (true)
                {
                    var context = await listener.GetContextAsync();
                    using var sink = new MemoryStream(); await context.Request.InputStream.CopyToAsync(sink);
                    captured.Enqueue(context.Request.Headers.AllKeys.Where(k => k != null)
                        .ToDictionary(k => k!, k => context.Request.Headers[k!]!, StringComparer.OrdinalIgnoreCase));
                    context.Response.StatusCode = 200; context.Response.ContentLength64 = 0; context.Response.Close();
                }
            }
            catch { }
        });
        var tracer = TraceAI.Register(opts =>
        {
            opts.ProjectName = "ambient-headers-audit"; opts.Verbose = false;
            opts.Endpoint = url; opts.ApiKey = "synthetic-fi-key"; opts.SecretKey = "synthetic-fi-secret";
            opts.Batch = false; opts.SetGlobalTracerProvider = false;
        });
        tracer.Trace("ambient-span", FISpanKind.Chain, _ => { });
        for (int i = 0; i < 100 && captured.IsEmpty; i++) await Task.Delay(50);
        TraceAI.Shutdown();
        listener.Stop();
        return Assert.Single(captured.Take(1));
    }

    [Theory]
    [InlineData("OTEL_EXPORTER_OTLP_HEADERS")]
    [InlineData("OTEL_EXPORTER_OTLP_TRACES_HEADERS")]
    public async Task AmbientOtlpHeaderVariableIsNotSentToFiEndpoint(string variable)
    {
        SetEnv("OTEL_EXPORTER_OTLP_HEADERS", null);
        SetEnv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", null);
        SetEnv(variable, Ambient);
        var headers = await ExportOneSpan();
        Assert.Equal("synthetic-fi-key", headers["X-Api-Key"]);
        Assert.Equal("synthetic-fi-secret", headers["X-Secret-Key"]);
        Assert.Equal("ambient-headers-audit", headers["fi-project-name"]);
        Assert.False(headers.ContainsKey("Authorization"));
        Assert.False(headers.ContainsKey("x-synthetic-marker"));
    }
}
