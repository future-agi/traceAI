using System.Text.RegularExpressions;

namespace FIInstrumentation;

/// <summary>A request that carries Future AGI keys was refused. The message names no value and no host.</summary>
public sealed class FiKeyRefusedException : InvalidOperationException
{
    /// <summary>FI_HOST_MISMATCH, FI_EMPTY_AUTHORITY, FI_TARGET_UNREADABLE, FI_PLAINTEXT_FI_HOST, FI_PLAINTEXT_NOT_ALLOWED or FI_AMBIENT_KEY_HEADERS.</summary>
    public string Code { get; }
    internal FiKeyRefusedException(string code, string message) : base(message) => Code = code;
}

/// <summary>What <see cref="FiKeyTransport.Decide"/> decided for one request.</summary>
/// <param name="Keys">The request carries Future AGI keys.</param>
/// <param name="Tls">The request is encrypted.</param>
/// <param name="PlaintextOptIn">A plaintext request that carries keys to a non-loopback host was allowed by FI_ALLOW_INSECURE_HTTP or the option.</param>
/// <param name="Endpoint">The endpoint to use: the target, with the scheme made https for a Future AGI gRPC host.</param>
public sealed record FiKeyDecision(bool Keys, bool Tls, bool PlaintextOptIn, bool FiHost, string Endpoint, string Origin);

/// <summary>
/// The one decision for every request that can carry Future AGI keys, taken on the target as written and the host the
/// transport will connect to (the two must agree), before any key header is attached:
/// <list type="bullet">
/// <item>a literal loopback host (localhost, 127.0.0.1, ::1, exactly as written) may use plaintext;</item>
/// <item>a Future AGI host (*.futureagi.com, one trailing dot allowed) always uses TLS: gRPC is made https, plaintext HTTP is refused;</item>
/// <item>a target whose written and parsed host disagree (a backslash before an @, %5C, an @ after the authority, an empty authority) is refused;</item>
/// <item>any other host over plaintext is refused unless FI_ALLOW_INSECURE_HTTP is exactly 1 or the option is set, with one warning per process;</item>
/// <item>a plaintext request that carries keys is sent with proxies off (loopback or opted in), and no key-carrying request follows a redirect (see <see cref="CreateHttpClient"/>).</item>
/// </list>
/// </summary>
public static class FiKeyTransport
{
    private static readonly Regex FiHost = new(@"(^|\.)futureagi\.com$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    private static readonly HashSet<string> LoopbackHosts = new(StringComparer.Ordinal) { "localhost", "127.0.0.1", "[::1]", "::1" };
    private static int s_warned;
    private static readonly object Gate = new();

    private const string MismatchMessage = "The endpoint names its host in a way parsers read differently (a backslash, a percent-encoded backslash or an @ outside the host part), so no Future AGI key is sent to it. Write the endpoint as scheme://host[:port]/path.";
    private const string EmptyAuthorityMessage = "The endpoint has an empty host, so no Future AGI key is sent to it. Write the endpoint as scheme://host[:port]/path.";
    private const string UnreadableMessage = "The endpoint is not an http or https URL, so no Future AGI key is sent to it. Write the endpoint as scheme://host[:port]/path.";
    private const string PlaintextFiMessage = "Future AGI keys are sent only over TLS: use an https:// endpoint for a Future AGI host.";
    private const string PlaintextNotAllowedMessage = "Future AGI keys are sent over a plaintext channel only to loopback. For a self-hosted collector without TLS, set FI_ALLOW_INSECURE_HTTP=1 or pass the allow-insecure-http option.";
    private const string AmbientMessage = "OTEL_EXPORTER_OTLP_HEADERS or OTEL_EXPORTER_OTLP_TRACES_HEADERS carries a Future AGI key header or a known credential, so no exporter is created for this collector. Remove it from those variables, or send the traces to Future AGI.";

    /// <summary>Whether <paramref name="host"/> (any case, one trailing dot allowed) is futureagi.com or one of its subdomains.</summary>
    public static bool IsFiHostName(string host) => FiHost.IsMatch(Comparable(host));

    private static string Comparable(string host)
    {
        var h = host.Trim().ToLowerInvariant();
        if (h.StartsWith('[') && h.EndsWith(']')) h = h[1..^1];
        return h.EndsWith('.') ? h[..^1] : h;
    }

    private static readonly Regex Special = new("^(?:https?|wss?|ftp|file)$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    private sealed record Parts(string Scheme, string Authority, string Rest, string RawAuthority);

    private static Parts? WrittenParts(string endpoint)
    {
        var text = endpoint.Trim();
        var m = Regex.Match(text, @"^([A-Za-z][A-Za-z0-9+.\-]*)://");
        if (!m.Success) return null;
        var scheme = m.Groups[1].Value;
        text = text[m.Length..];
        var special = Special.IsMatch(scheme);
        var end = text.IndexOfAny(special ? new[] { '/', '?', '#', '\\' } : new[] { '/', '?', '#' });
        var authority = end < 0 ? text : text[..end];
        var raw = text.IndexOfAny(new[] { '/', '?', '#' });
        return new Parts(scheme, authority, end < 0 ? "" : text[end..], raw < 0 ? text : text[..raw]);
    }

    /// <summary>The host as written (userinfo, port, path, query and fragment removed, lower case, IPv6 brackets kept), or null.</summary>
    internal static string? WrittenHost(string endpoint)
    {
        var parts = WrittenParts(endpoint);
        if (parts == null) return null;
        var hostport = parts.Authority[(parts.Authority.LastIndexOf('@') + 1)..];
        if (hostport.StartsWith('['))
        {
            var close = hostport.IndexOf(']');
            return close < 0 ? null : hostport[..(close + 1)].ToLowerInvariant();
        }
        var colon = hostport.LastIndexOf(':');
        var host = colon < 0 ? hostport : hostport[..colon];
        return host.Length == 0 ? null : host.ToLowerInvariant();
    }

    /// <summary>True only for the literal loopback spellings, as written, when the host the transport connects to is the same host.</summary>
    internal static bool IsLiteralLoopback(string endpoint, Uri parsed)
    {
        var written = WrittenHost(endpoint);
        return written != null && LoopbackHosts.Contains(written) && Comparable(written) == Comparable(parsed.Host);
    }

    /// <summary>
    /// Decides one request. <paramref name="keys"/> says whether the outgoing set carries a Future AGI key (either of the pair counts).
    /// Throws <see cref="FiKeyRefusedException"/>, naming no value, when the keys may not go to that destination.
    /// </summary>
    public static FiKeyDecision Decide(string target, bool grpc, bool keys, bool allowInsecureHttp = false)
    {
        if (!keys) return new FiKeyDecision(false, target.TrimStart().StartsWith("https:", StringComparison.OrdinalIgnoreCase), false, false, target, "");
        var parts = WrittenParts(target);
        if (parts == null) throw new FiKeyRefusedException("FI_TARGET_UNREADABLE", UnreadableMessage);
        // The written form decides first: an empty authority and a host a second parser reads differently are named for what they are,
        // whether or not .NET happens to parse the same text.
        if (parts.Authority.Length == 0) throw new FiKeyRefusedException("FI_EMPTY_AUTHORITY", EmptyAuthorityMessage);
        var ambiguous = Regex.IsMatch(parts.RawAuthority, @"\\|%5c", RegexOptions.IgnoreCase) || parts.Rest.Contains('@');
        if (ambiguous) throw new FiKeyRefusedException("FI_HOST_MISMATCH", MismatchMessage);
        if (!Uri.TryCreate(target.Trim(), UriKind.Absolute, out var uri) || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            throw new FiKeyRefusedException("FI_TARGET_UNREADABLE", UnreadableMessage);
        var written = WrittenHost(target);
        var fiParsed = IsFiHostName(uri.Host);
        var fiWritten = written != null && IsFiHostName(written);
        if (fiParsed != fiWritten || written == null) throw new FiKeyRefusedException("FI_HOST_MISMATCH", MismatchMessage);
        var origin = $"{uri.Scheme}://{Comparable(uri.Host)}:{uri.Port}";
        var tls = uri.Scheme == Uri.UriSchemeHttps;
        var endpoint = target;
        if (fiParsed)
        {
            // A Future AGI host always uses TLS: gRPC is made https, plaintext HTTP is refused and no option changes that.
            if (!tls && !grpc) throw new FiKeyRefusedException("FI_PLAINTEXT_FI_HOST", PlaintextFiMessage);
            if (!tls) { endpoint = new UriBuilder(uri) { Scheme = Uri.UriSchemeHttps, Port = uri.IsDefaultPort ? -1 : uri.Port }.Uri.ToString(); tls = true; origin = $"https://{Comparable(uri.Host)}:{(uri.IsDefaultPort ? 443 : uri.Port)}"; }
            return new FiKeyDecision(true, true, false, true, endpoint, origin);
        }
        if (tls) return new FiKeyDecision(true, true, false, false, endpoint, origin);
        if (IsLiteralLoopback(target, uri)) return new FiKeyDecision(true, false, false, false, endpoint, origin);
        if (!allowInsecureHttp && Environment.GetEnvironmentVariable("FI_ALLOW_INSECURE_HTTP") != "1") throw new FiKeyRefusedException("FI_PLAINTEXT_NOT_ALLOWED", PlaintextNotAllowedMessage);
        WarnOnce(origin);
        return new FiKeyDecision(true, false, true, false, endpoint, origin);
    }

    private static void WarnOnce(string origin)
    {
        lock (Gate)
        {
            if (s_warned == 1) return;
            s_warned = 1;
        }
        try { Console.Error.WriteLine($"[traceAI] Sending Future AGI keys in plaintext to {origin} because FI_ALLOW_INSECURE_HTTP or the allow-insecure-http option is set."); } catch { }
    }

    /// <summary>
    /// An HTTP client for a request that carries keys: it never follows a redirect (a 3xx answer is a failed export and nothing
    /// goes to the redirect target) and, for a plaintext request sent under the opt-in, it never uses a proxy.
    /// </summary>
    public static HttpClient CreateHttpClient(FiKeyDecision decision) => new(CreateHandler(decision), disposeHandler: true);

    /// <summary>The handler behind <see cref="CreateHttpClient"/>: no redirect, and no proxy for any plaintext request that carries keys. An https request keeps the normal proxy behaviour.</summary>
    internal static SocketsHttpHandler CreateHandler(FiKeyDecision decision)
    {
        var handler = new SocketsHttpHandler { AllowAutoRedirect = false };
        // No plaintext request that carries keys goes through a proxy, loopback or not, with or without the opt-in.
        if (decision.Keys && !decision.Tls) handler.UseProxy = false;
        return handler;
    }

    /// <summary>A client for a request that may carry keys, built from the decision (keys or not, no redirect is ever followed).</summary>
    public static Func<HttpClient> HttpClientFactory(FiKeyDecision decision) => () => CreateHttpClient(decision);

    /// <summary>The same factory with the exporter's timeout applied to each client (the exporter's own default factory sets it; a custom one must).</summary>
    public static Func<HttpClient> HttpClientFactory(FiKeyDecision decision, int timeoutMilliseconds) => () =>
    {
        var client = CreateHttpClient(decision);
        client.Timeout = TimeSpan.FromMilliseconds(timeoutMilliseconds);
        return client;
    };

    /// <summary>
    /// An exporter for a collector that is not Future AGI would merge OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_TRACES_HEADERS into what
    /// it sends. Refuse when they carry an x-api-key or x-secret-key header name, or a header value that contains a Future AGI key or another
    /// known secret (<paramref name="knownSecrets"/>, and FI_API_KEY / FI_SECRET_KEY from the environment).
    /// </summary>
    public static void AssertNoFiKeysInAmbientHeaders(IEnumerable<string>? knownSecrets = null)
    {
        var raw = new[] { Environment.GetEnvironmentVariable("OTEL_EXPORTER_OTLP_HEADERS"), Environment.GetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_HEADERS") }
            .Where(v => !string.IsNullOrWhiteSpace(v)).Select(v => v!).ToArray();
        if (raw.Length == 0) return;
        var values = new List<string>();
        foreach (var text in raw)
        {
            values.Add(text);
            foreach (var pair in text.Split(',', StringSplitOptions.RemoveEmptyEntries))
            {
                var split = pair.IndexOf('=');
                if (split <= 0) continue;
                var name = pair[..split].Trim();
                if (name.Equals("x-api-key", StringComparison.OrdinalIgnoreCase) || name.Equals("x-secret-key", StringComparison.OrdinalIgnoreCase))
                    throw new FiKeyRefusedException("FI_AMBIENT_KEY_HEADERS", AmbientMessage);
                var value = pair[(split + 1)..].Trim();
                values.Add(value);
                try { values.Add(Uri.UnescapeDataString(value)); } catch (UriFormatException) { }
            }
        }
        var secrets = new HashSet<string>(knownSecrets ?? Array.Empty<string>(), StringComparer.Ordinal);
        foreach (var name in new[] { "FI_API_KEY", "FI_SECRET_KEY" })
            if (Environment.GetEnvironmentVariable(name) is { } v && v.Trim().Length >= 8) secrets.Add(v);
        foreach (var secret in secrets)
            if (secret.Length >= 8 && values.Any(value => value.Contains(secret, StringComparison.Ordinal))) throw new FiKeyRefusedException("FI_AMBIENT_KEY_HEADERS", AmbientMessage);
    }
}
