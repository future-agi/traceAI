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
import java.util.concurrent.TimeUnit;
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
        String written;
        if (authority.startsWith("[")) {
            int close = authority.indexOf(']');
            if (close < 0) throw new Refused("the target host is ambiguous");
            written = authority.substring(1, close);
            checkBracketedHost(written);
            checkPort(authority.substring(close + 1));
        } else {
            for (int i = 0; i < authority.length(); i++) {
                char c = authority.charAt(i);
                if (c == '@' || c == '\\' || c == '%' || c <= ' ' || c >= 0x7f) throw new Refused("the target host is ambiguous");
            }
            int colon = authority.indexOf(':');
            written = colon < 0 ? authority : authority.substring(0, colon);
            checkPort(colon < 0 ? "" : authority.substring(colon));
        }
        // After the authority an '@', a backslash or an escaped backslash can make another parser read a different host.
        String rest = url.substring(end);
        if (rest.indexOf('@') >= 0 || rest.indexOf('\\') >= 0 || rest.toLowerCase(Locale.ROOT).contains("%5c")) throw new Refused("the target path or query is ambiguous");
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

    /**
     * The inside of an IPv6 literal. A zone is written {@code %25<zone>} and is the only percent escape allowed; the written host keeps it
     * exactly as the URL parser does. A zoned address is link-local and never loopback.
     */
    private static void checkBracketedHost(String inner) {
        int percent = inner.indexOf('%');
        String address = percent < 0 ? inner : inner.substring(0, percent);
        if (address.isEmpty()) throw new Refused("the target host is ambiguous");
        for (int i = 0; i < address.length(); i++) {
            char c = address.charAt(i);
            if (!(c == ':' || c == '.' || Character.digit(c, 16) >= 0)) throw new Refused("the target host is ambiguous");
        }
        if (percent < 0) return;
        if (!inner.startsWith("%25", percent) || inner.length() == percent + 3) throw new Refused("the target host is ambiguous");
        for (int i = percent + 3; i < inner.length(); i++) {
            char c = inner.charAt(i);
            if (!(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '.' || c == '_' || c == '-' || c == '~')) throw new Refused("the target host is ambiguous");
        }
    }

    private static void checkPort(String rest) {
        if (rest.isEmpty()) return;
        if (rest.charAt(0) != ':' || rest.length() > 6) throw new Refused("the target port is invalid");
        for (int i = 1; i < rest.length(); i++) if (rest.charAt(i) < '0' || rest.charAt(i) > '9') throw new Refused("the target port is invalid");
    }

    /** A client for key-carrying sends: never follows redirects, and uses no proxy for plaintext. */
    public static HttpClient client(Target target, Duration connectTimeout) { return client(target, connectTimeout, null); }

    /** As {@link #client(Target, Duration)}, running on {@code executor} (which the caller shuts down with the exporter), when given. */
    public static HttpClient client(Target target, Duration connectTimeout, java.util.concurrent.Executor executor) {
        HttpClient.Builder builder = HttpClient.newBuilder().connectTimeout(connectTimeout).followRedirects(HttpClient.Redirect.NEVER);
        if (target.plaintext()) builder.proxy(NO_PROXY);
        if (executor != null) builder.executor(executor);
        return builder.build();
    }

    /** A daemon executor for one exporter's client. */
    public static java.util.concurrent.ExecutorService newExecutor() {
        java.util.concurrent.atomic.AtomicInteger count = new java.util.concurrent.atomic.AtomicInteger();
        return java.util.concurrent.Executors.newCachedThreadPool(runnable -> {
            Thread thread = new Thread(runnable, "traceai-export-" + count.incrementAndGet()); thread.setDaemon(true); return thread;
        });
    }

    /** Releases a client and its executor: the JDK's own shutdown where the client has one (JDK 21), and the executor always. */
    public static void release(HttpClient client, java.util.concurrent.ExecutorService executor) {
        try { HttpClient.class.getMethod("shutdownNow").invoke(client); } catch (ReflectiveOperationException | RuntimeException unavailable) { /* JDK 17 */ }
        if (executor != null) executor.shutdownNow();
    }

    /**
     * The sends of one exporter that are in flight. Flush and shutdown wait for them, up to a limit, and report failure when one is
     * still running; after {@link #close()} no send begins.
     */
    public static final class Sends {
        private final Object lock = new Object();
        private int inFlight;
        private boolean closed;
        /** Starts a send; false (send nothing) once closed. */
        public boolean begin() { synchronized (lock) { if (closed) return false; inFlight++; return true; } }
        public void end() { synchronized (lock) { inFlight--; lock.notifyAll(); } }
        /** Waits until no send is in flight; false when one still is after {@code limit}. */
        public boolean awaitIdle(Duration limit) {
            long deadline = System.nanoTime() + limit.toNanos();
            synchronized (lock) {
                while (inFlight > 0) {
                    long left = deadline - System.nanoTime();
                    if (left <= 0) return false;
                    try { TimeUnit.NANOSECONDS.timedWait(lock, left); } catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); return false; }
                }
                return true;
            }
        }
        public void close() { synchronized (lock) { closed = true; } }
        public boolean closed() { synchronized (lock) { return closed; } }
    }

    /** A 3xx answer to a key-carrying request is a refusal; nothing is sent to the redirect target. */
    public static void checkStatus(int status) {
        if (status >= 300 && status < 400) throw new Refused("the collector answered with a redirect");
    }

    /**
     * Refuses a configuration whose ambient OTLP headers (OTEL_EXPORTER_OTLP_HEADERS, OTEL_EXPORTER_OTLP_TRACES_HEADERS in
     * {@code env}) name a Future AGI key header (X-Api-Key, X-Secret-Key: any case, trimmed, percent-decoded, in any comma-separated
     * pair) or carry a Future AGI key value ({@code fiValues}, as written or percent-decoded). Another vendor's values and other
     * credential variables are not judged. A variable that cannot be parsed fails closed only when {@code fiValues} is not empty.
     * The message names no value.
     */
    public static void checkAmbientHeaders(Map<String, String> env, Collection<String> fiValues) {
        if (env == null) return;
        List<String> keys = new java.util.ArrayList<>();
        if (fiValues != null) for (String value : fiValues) if (value != null && value.trim().length() >= 8) { keys.add(value); keys.add(value.trim()); }
        boolean configured = fiValues != null && !fiValues.isEmpty();
        for (String variable : new String[] {"OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"}) {
            String raw = env.get(variable);
            if (raw == null || raw.isBlank()) continue;
            boolean malformed = false;
            for (String pair : raw.split(",", -1)) {
                String trimmed = pair.trim();
                int equals = trimmed.indexOf('=');
                String name = equals < 0 ? "" : trimmed.substring(0, equals).trim();
                String decoded = decode(name);
                if (equals < 0 || name.isEmpty() || decoded == null) { malformed = true; continue; }
                String lower = decoded.trim().toLowerCase(Locale.ROOT);
                if (lower.equals("x-api-key") || lower.equals("x-secret-key") || name.toLowerCase(Locale.ROOT).equals("x-api-key") || name.toLowerCase(Locale.ROOT).equals("x-secret-key"))
                    throw new Refused("ambient OTLP headers name a Future AGI key header");
                if (decode(trimmed.substring(equals + 1).trim()) == null) malformed = true;
            }
            String whole = decodeLenient(raw);
            for (String key : keys) {
                if (raw.contains(key) || whole.contains(key)) throw new Refused("ambient OTLP headers carry a Future AGI key");
            }
            if (malformed && configured) throw new Refused("ambient OTLP headers cannot be read while Future AGI keys are configured");
        }
    }

    /** Percent-decodes (UTF-8); null when an escape is malformed. */
    private static String decode(String text) {
        try { return java.net.URLDecoder.decode(text.replace("+", "%2B"), java.nio.charset.StandardCharsets.UTF_8); }
        catch (IllegalArgumentException malformed) { return null; }
    }

    /** Percent-decodes what can be decoded and keeps a malformed escape as written. */
    private static String decodeLenient(String text) {
        String decoded = decode(text);
        if (decoded != null) return decoded;
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == '%' && i + 2 < text.length() + 0 && Character.digit(text.charAt(i + 1), 16) >= 0 && Character.digit(text.charAt(i + 2), 16) >= 0) {
                out.append((char) Integer.parseInt(text.substring(i + 1, i + 3), 16)); i += 2;
            } else out.append(c);
        }
        return out.toString();
    }

    /** Test hook: allows the one-per-process warning to be observed again. */
    public static void resetWarningForTest() { WARNED.set(false); }
}
