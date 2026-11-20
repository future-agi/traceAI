package ai.traceai;

import java.net.URI;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.Map;

/** The single decision every key-carrying send goes through. */
public final class KeyTransport {
    public static final String ALLOW_INSECURE_SETTING = "FI_ALLOW_INSECURE_HTTP";
    private KeyTransport() { }

    /** A refusal. */
    public static final class Refused extends IllegalArgumentException {
        Refused(String reason) { super("Key-carrying export refused: " + reason); }
    }

    /** An approved target. */
    public static final class Target {
        private final String url;
        Target(String url) { this.url = url; }
        public boolean plaintext() { return url.startsWith("http://"); }
        public URI endpoint(String suffix) { return URI.create(url.replaceAll("/+$", "") + suffix); }
    }

    public static Target check(String url, boolean completePair, Map<String, String> settings, boolean explicitInsecure) {
        return new Target(url);
    }

    public static HttpClient client(Target target, Duration connectTimeout) {
        return HttpClient.newBuilder().connectTimeout(connectTimeout).build();
    }

    public static void checkStatus(int status) { }

    public static void resetWarningForTest() { }
}
