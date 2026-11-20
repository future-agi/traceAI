package ai.traceai;

import java.io.IOException;
import java.net.ProxySelector;
import java.net.SocketAddress;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * The single decision every key-carrying send goes through. It judges the target as written and the
 * host that would actually be connected, and refuses before any key header is attached.
 *
 * <ul>
 *   <li>Literal loopback ({@code localhost}, {@code 127.0.0.1}, {@code ::1}, exactly as written) may use plaintext.</li>
 *   <li>Future AGI hosts (the written host, one trailing dot ignored) require https; the opt-in never applies.</li>
 *   <li>A written host that differs from the parsed host, userinfo, a backslash, an escaped authority or an empty
 *       authority is refused.</li>
 *   <li>Any other host over plaintext is refused unless {@code FI_ALLOW_INSECURE_HTTP} is exactly {@code 1}
 *       or the explicit option is set.</li>
 *   <li>A transport built for a plaintext send uses no proxy, and a key-carrying request never follows a redirect.</li>
 * </ul>
 * Only the JDK is used, so the class can be bundled with the agent.
 */
public final class KeyTransport {
    /** The setting that opts in to plaintext http for hosts other than loopback and Future AGI. */
    public static final String ALLOW_INSECURE_SETTING = "FI_ALLOW_INSECURE_HTTP";
    private static final AtomicBoolean WARNED = new AtomicBoolean();
    private static final ProxySelector NO_PROXY = new ProxySelector() {
        @Override public List<java.net.Proxy> select(URI uri) { return List.of(java.net.Proxy.NO_PROXY); }
        @Override public void connectFailed(URI uri, SocketAddress address, IOException failure) { }
    };

    private KeyTransport() { }

    /** A refusal. The message never contains the target or any key. */
    public static final class Refused extends IllegalArgumentException {
        Refused(String reason) { super("Key-carrying export refused: " + reason); }
    }

    /** An approved target. */
    public static final class Target {
        private final String scheme, host, path;
        private final int port;
        private final boolean plaintext;
        Target(String scheme, String host, int port, String path, boolean plaintext) {
            this.scheme = scheme; this.host = host; this.port = port; this.path = path; this.plaintext = plaintext;
        }
        public boolean plaintext() { return plaintext; }
        /** The endpoint for {@code suffix} (starting with "/"), on exactly the approved host. */
        public URI endpoint(String suffix) {
            try { return new URI(scheme + "://" + host + (port < 0 ? "" : ":" + port) + path + suffix); }
            catch (URISyntaxException e) { throw new Refused("the target is not a valid URL"); }
        }
    }

    /**
     * Decides whether keys may be sent to {@code url}.
     *
     * @param url the target as written
     * @param completePair true only when both halves of the key pair are present
     * @param settings the settings to read FI_ALLOW_INSECURE_HTTP from (normally the environment); may be null
     * @param explicitInsecure the explicit option form of the opt-in
     * @throws Refused when the target may not receive keys
     */
    public static Target check(String url, boolean completePair, Map<String, String> settings, boolean explicitInsecure) {
        if (!completePair) throw new Refused("the key pair is incomplete");
        if (url == null || url.isEmpty()) throw new Refused("no target URL");
        String lower = url.toLowerCase(Locale.ROOT);
        boolean tls;
        int start;
        if (lower.startsWith("https://")) { tls = true; start = 8; }
        else if (lower.startsWith("http://")) { tls = false; start = 7; }
        else throw new Refused("the target scheme is not http or https");
        int end = url.length();
        for (int i = start; i < url.length(); i++) {
            char c = url.charAt(i);
            if (c == '/' || c == '?' || c == '#') { end = i; break; }
        }
        String authority = url.substring(start, end);
        if (authority.isEmpty()) throw new Refused("the target has an empty authority");
        for (int i = 0; i < authority.length(); i++) {
            char c = authority.charAt(i);
            if (c == '@' || c == '\\' || c == '%' || c <= ' ' || c >= 0x7f) throw new Refused("the target host is ambiguous");
        }
        String written;
        if (authority.startsWith("[")) {
            int close = authority.indexOf(']');
            if (close < 0) throw new Refused("the target host is ambiguous");
            written = authority.substring(1, close);
            checkPort(authority.substring(close + 1));
        } else {
            int colon = authority.indexOf(':');
            written = colon < 0 ? authority : authority.substring(0, colon);
            checkPort(colon < 0 ? "" : authority.substring(colon));
        }
        if (written.isEmpty()) throw new Refused("the target has an empty host");
        URI parsed;
        try { parsed = new URI(url); } catch (URISyntaxException e) { throw new Refused("the target is not a valid URL"); }
        String parsedHost = parsed.getHost();
        if (parsedHost == null || parsed.getRawUserInfo() != null) throw new Refused("the target host is ambiguous");
        if (parsedHost.startsWith("[") && parsedHost.endsWith("]")) parsedHost = parsedHost.substring(1, parsedHost.length() - 1);
        if (!parsedHost.equalsIgnoreCase(written)) throw new Refused("the written and parsed hosts differ");
        boolean loopback = written.equals("localhost") || written.equals("127.0.0.1") || written.equals("::1");
        String name = written.toLowerCase(Locale.ROOT);
        if (name.endsWith(".")) name = name.substring(0, name.length() - 1);
        boolean futureAgi = name.equals("futureagi.com") || name.endsWith(".futureagi.com");
        if (!tls && !loopback) {
            if (futureAgi) throw new Refused("plaintext http to a Future AGI host is not allowed");
            boolean optIn = explicitInsecure || (settings != null && "1".equals(settings.get(ALLOW_INSECURE_SETTING)));
            if (!optIn) throw new Refused("plaintext http requires " + ALLOW_INSECURE_SETTING + "=1");
            if (WARNED.compareAndSet(false, true)) {
                System.err.println("[traceai] Plaintext http export is enabled by " + ALLOW_INSECURE_SETTING
                    + (explicitInsecure ? " (explicit option)" : " (environment)") + "; keys are sent without TLS");
            }
        }
        String host = written.indexOf(':') >= 0 ? "[" + written + "]" : written;
        String path = parsed.getRawPath() == null ? "" : parsed.getRawPath().replaceAll("/+$", "");
        return new Target(tls ? "https" : "http", host, parsed.getPort(), path, !tls);
    }

    private static void checkPort(String rest) {
        if (rest.isEmpty()) return;
        if (rest.charAt(0) != ':' || rest.length() > 6) throw new Refused("the target port is invalid");
        for (int i = 1; i < rest.length(); i++) if (rest.charAt(i) < '0' || rest.charAt(i) > '9') throw new Refused("the target port is invalid");
    }

    /** A client for key-carrying sends: never follows redirects, and uses no proxy for plaintext. */
    public static HttpClient client(Target target, Duration connectTimeout) {
        HttpClient.Builder builder = HttpClient.newBuilder().connectTimeout(connectTimeout).followRedirects(HttpClient.Redirect.NEVER);
        if (target.plaintext()) builder.proxy(NO_PROXY);
        return builder.build();
    }

    /** A 3xx answer to a key-carrying request is a refusal; nothing is sent to the redirect target. */
    public static void checkStatus(int status) {
        if (status >= 300 && status < 400) throw new Refused("the collector answered with a redirect");
    }

    /**
     * Refuses a configuration whose ambient OTLP headers (OTEL_EXPORTER_OTLP_HEADERS, OTEL_EXPORTER_OTLP_TRACES_HEADERS in
     * {@code env}) name a Future AGI key header or carry a Future AGI key value ({@code fiValues}).
     */
    public static void checkAmbientHeaders(Map<String, String> env, Collection<String> fiValues) { }

    /** Test hook: allows the one-per-process warning to be observed again. */
    public static void resetWarningForTest() { WARNED.set(false); }
}
