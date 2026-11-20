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

    [Theory]
    [InlineData("http://api.futureagi.com\\@collector.example", "FI_HOST_MISMATCH")]
    [InlineData("http://collector.example%5C@api.futureagi.com", "FI_HOST_MISMATCH")]
    [InlineData("http://collector.example/x@api.futureagi.com", "FI_HOST_MISMATCH")]
    [InlineData("http://localhost\\@collector.example:4318", "FI_HOST_MISMATCH")]
    [InlineData("https://api.futureagi.com\\@collector.example", "FI_HOST_MISMATCH")]
    [InlineData("http:///api.futureagi.com", "FI_EMPTY_AUTHORITY")]
    [InlineData("https:///api.futureagi.com/", "FI_EMPTY_AUTHORITY")]
    [InlineData("https://\\api.futureagi.com/", "FI_EMPTY_AUTHORITY")]
    public void AnAmbiguousTargetIsRefusedWithItsOwnCode(string target, string code)
    {
        foreach (var optIn in new[] { false, true })
            Assert.Equal(code, Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.Decide(target, false, true, allowInsecureHttp: optIn)).Code);
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
    [InlineData("http://127.0.0.1:4318", false)]
    [InlineData("http://localhost:4318", false)]
    [InlineData("http://[::1]:4318", false)]
    [InlineData("http://127.1:4318", true)]
    [InlineData("http://collector.example:4318", true)]
    public void NoPlaintextKeyCarryingClientEverUsesAProxy(string target, bool optIn)
    {
        var decision = FiKeyTransport.Decide(target, false, true, allowInsecureHttp: optIn);
        Assert.False(decision.Tls);
        Assert.False(FiKeyTransport.CreateHandler(decision).UseProxy);
        Assert.False(FiKeyTransport.CreateHandler(decision).AllowAutoRedirect);
        // The same targets with the opt-in set do not change it for loopback either.
        if (!optIn) Assert.False(FiKeyTransport.CreateHandler(FiKeyTransport.Decide(target, false, true, allowInsecureHttp: true)).UseProxy);
    }

    [Fact]
    public void AClientForARequestWithNoKeysIsNotChangedByTheProxyRule()
    {
        Assert.True(FiKeyTransport.CreateHandler(FiKeyTransport.Decide("http://collector.example:4318", false, false)).UseProxy);
    }

    [Theory]
    [InlineData(1500)]
    [InlineData(2000)]
    [InlineData(10000)]
    public void TheClientFactoryKeepsTheExporterTimeout(int milliseconds)
    {
        var decision = FiKeyTransport.Decide("https://collector.example:4318", false, true);
        using var client = FiKeyTransport.HttpClientFactory(decision, milliseconds)();
        Assert.Equal(TimeSpan.FromMilliseconds(milliseconds), client.Timeout);
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
    public void AnotherVendorsCredentialInTheAmbientHeadersIsNotAKnownFutureAgiSecret()
    {
        // Only Future AGI key values (environment, options) and secrets registered through Future AGI's own options count; a
        // credential-named variable of another vendor does not, so that vendor's collector keeps working.
        Environment.SetEnvironmentVariable("X_VENDOR_API_KEY", "vendor-key-synthetic-0002");
        try
        {
            Environment.SetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS", "x-vendor-team=vendor-key-synthetic-0002");
            FiKeyTransport.AssertNoFiKeysInAmbientHeaders();
            Environment.SetEnvironmentVariable("FI_SECRET_KEY", "vendor-key-synthetic-0002");
            Assert.Equal("FI_AMBIENT_KEY_HEADERS", Assert.Throws<FiKeyRefusedException>(() => FiKeyTransport.AssertNoFiKeysInAmbientHeaders()).Code);
        }
        finally { Environment.SetEnvironmentVariable("X_VENDOR_API_KEY", null); }
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
