namespace FIInstrumentation.Tests;

/// <summary>The one decision (FiKeyTransport.Decide), its handler and the ambient-header screen, without any network.</summary>
public class KeyTransportDecisionTests : IDisposable
{
    private static readonly string[] Names = { "FI_ALLOW_INSECURE_HTTP", "FI_API_KEY", "FI_SECRET_KEY", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS" };
    private readonly string?[] _env = Names.Select(Environment.GetEnvironmentVariable).ToArray();
    public KeyTransportDecisionTests() { foreach (var n in Names) Environment.SetEnvironmentVariable(n, null); }
    public void Dispose() { for (var i = 0; i < Names.Length; i++) Environment.SetEnvironmentVariable(Names[i], _env[i]); }

    [Theory]
    [InlineData("https://api.futureagi.com", false, true, false, "https://api.futureagi.com")]
    [InlineData("http://127.0.0.1:4318", false, false, false, "http://127.0.0.1:4318")]
    [InlineData("http://localhost:4318", false, false, false, "http://localhost:4318")]
    [InlineData("http://[::1]:4318", false, false, false, "http://[::1]:4318")]
    public void TheDecisionForAllowedTargets(string target, bool grpc, bool tls, bool optIn, string expectedEndpoint)
    {
        var decision = FiKeyTransport.Decide(target, grpc, keys: true);
        Assert.Equal(tls, decision.Tls);
        Assert.Equal(optIn, decision.PlaintextOptIn);
        Assert.StartsWith(expectedEndpoint, decision.Endpoint, StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("http://grpc.futureagi.com:4317")]
    [InlineData("http://grpc.futureagi.com.:4317")]
    public void AGrpcFutureAgiHostAlwaysUsesTls(string target)
    {
        var decision = FiKeyTransport.Decide(target, grpc: true, keys: true, allowInsecureHttp: true);
        Assert.True(decision.Tls);
        Assert.StartsWith("https://", decision.Endpoint);
        Assert.False(decision.PlaintextOptIn);
    }

    [Theory]
    [InlineData("127.1")]
    [InlineData("0x7f.0.0.1")]
    [InlineData("2130706433")]
    [InlineData("127.0.0.1.")]
    public void OnlyTheLiteralLoopbackSpellingsAreLoopback(string host)
    {
        Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.Decide($"http://{host}:4318", grpc: false, keys: true));
        var decision = FiKeyTransport.Decide($"http://{host}:4318", grpc: false, keys: true, allowInsecureHttp: true);
        Assert.True(decision.PlaintextOptIn);
    }

    [Fact]
    public void NoKeysNoDecision()
    {
        var decision = FiKeyTransport.Decide("http://collector.example:4318", grpc: false, keys: false);
        Assert.False(decision.Keys);
    }

    [Fact]
    public void TheRefusalsNameNoValue()
    {
        foreach (var target in new[] { "http://api.futureagi.com", "http://collector.example:4318", "http:///x", "http://api.futureagi.com\\@collector.example" })
        {
            var error = Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.Decide(target, grpc: false, keys: true));
            Assert.DoesNotContain("futureagi", error.Message);
            Assert.DoesNotContain("collector.example", error.Message);
            Assert.Matches("^FI_", error.Code);
        }
    }

    [Fact]
    public void AKeyCarryingClientNeverFollowsRedirectsAndAPlaintextOptInClientNeverUsesAProxy()
    {
        var https = FiKeyTransport.Decide("https://collector.example:4318", false, true);
        var optIn = FiKeyTransport.Decide("http://127.1:4318", false, true, allowInsecureHttp: true);
        var tls = FiKeyTransport.CreateHandler(https); var plain = FiKeyTransport.CreateHandler(optIn);
        Assert.False(tls.AllowAutoRedirect); Assert.False(plain.AllowAutoRedirect);
        Assert.True(tls.UseProxy);
        Assert.False(plain.UseProxy);
    }

    [Theory]
    [InlineData("x-api-key=ambient-key-synthetic-0001")]
    [InlineData("X-Secret-Key=ambient-secret-synthetic-0001,authorization=Bearer%20x")]
    public void AmbientHeadersWithAKeyHeaderAreRefusedForAnotherCollector(string headers)
    {
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", headers);
        Assert.Equal("FI_AMBIENT_KEY_HEADERS", Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.AssertNoFiKeysInAmbientHeaders()).Code);
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", null);
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_HEADERS", headers);
        Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.AssertNoFiKeysInAmbientHeaders());
    }

    [Fact]
    public void AKnownSecretUnderAnotherHeaderNameIsRefusedAndAnUnrelatedHeaderIsKept()
    {
        Environment.SetEnvironmentVariable("FI_SECRET_KEY", "env-secret-synthetic-0003");
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=prefix-env-secret-synthetic-0003");
        Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.AssertNoFiKeysInAmbientHeaders());
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=registered-secret-synthetic-0004");
        Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.AssertNoFiKeysInAmbientHeaders(new[] { "registered-secret-synthetic-0004" }));
        FiKeyTransport.AssertNoFiKeysInAmbientHeaders();
        Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer%20other-backend-token");
        FiKeyTransport.AssertNoFiKeysInAmbientHeaders(new[] { "registered-secret-synthetic-0004" });
    }
}
