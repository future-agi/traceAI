package ai.traceai;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** One cell per rule of the key-transport decision. */
class KeyTransportTest {
    private static final Map<String, String> NONE = Map.of();
    private static final Map<String, String> OPT_IN = Map.of("FI_ALLOW_INSECURE_HTTP", "1");
    private PrintStream originalErr;
    private ByteArrayOutputStream err;

    @BeforeEach void capture() {
        KeyTransport.resetWarningForTest();
        originalErr = System.err; err = new ByteArrayOutputStream(); System.setErr(new PrintStream(err, true));
    }
    @AfterEach void restore() { System.setErr(originalErr); KeyTransport.resetWarningForTest(); }

    private static void refused(String url) { refused(url, NONE, false); }
    private static void refused(String url, Map<String, String> settings, boolean explicit) {
        assertThatThrownBy(() -> KeyTransport.check(url, true, settings, explicit))
            .isInstanceOf(KeyTransport.Refused.class)
            .hasMessageNotContaining("evil").hasMessageNotContaining("futureagi");
    }

    @ParameterizedTest
    @ValueSource(strings = {"http://localhost:8080", "http://127.0.0.1:4318/base", "http://[::1]:4318", "http://localhost"})
    void literalLoopbackMayUsePlaintextWithoutOptIn(String url) {
        KeyTransport.Target target = KeyTransport.check(url, true, NONE, false);
        assertThat(target.plaintext()).isTrue();
        assertThat(err.toString()).isEmpty();
    }

    @Test void loopbackSpelledAnyOtherWayNeedsTheOptIn() {
        for (String url : new String[] {"http://LOCALHOST:1", "http://localhost.:1", "http://127.0.0.2:1", "http://0.0.0.0:1", "http://[0:0:0:0:0:0:0:1]:1"})
            refused(url);
    }

    @Test void futureAgiOverPlaintextIsRefusedWithAndWithoutOptIn() {
        for (String url : new String[] {"http://api.futureagi.com", "http://api.futureagi.com.", "http://API.FutureAGI.com/x", "http://eu.api.futureagi.com:80"}) {
            refused(url);
            refused(url, OPT_IN, true);
        }
    }

    @Test void futureAgiOverTlsIsAllowedIncludingTrailingDot() {
        assertThat(KeyTransport.check("https://api.futureagi.com", true, NONE, false).plaintext()).isFalse();
        assertThat(KeyTransport.check("https://api.futureagi.com.", true, NONE, false).plaintext()).isFalse();
    }

    @Test void lookAlikeOfFutureAgiIsAnOrdinaryHost() {
        refused("http://api.futureagi.com.evil.example");
        refused("http://notfutureagi.com");
        assertThat(KeyTransport.check("https://futureagi.com.evil.example", true, NONE, false).plaintext()).isFalse();
    }

    @ParameterizedTest
    @ValueSource(strings = {
        "http://localhost\\@evil.example/", "https://api.futureagi.com\\@evil.example/",
        "http://localhost%5C@evil.example/", "https://api.futureagi.com%5c@evil.example",
        "http://localhost:80@evil.example/", "https://api.futureagi.com@evil.example",
        "https://evil.example@api.futureagi.com", "http://localhost@127.0.0.1",
        "http:///evil.example", "https:///api.futureagi.com", "http://", "https://:443",
        "http://localhost/\\@evil.example", "http://local host", "ftp://localhost", "localhost:4318", ""})
    void ambiguousOrDisagreeingTargetsAreRefused(String url) {
        refused(url, OPT_IN, true);
        refused(url);
    }

    @Test void selfHostedPlaintextIsRefusedByDefault() {
        refused("http://collector.internal:4318");
        refused("http://collector.internal:4318", Map.of("FI_ALLOW_INSECURE_HTTP", "true"), false);
        refused("http://collector.internal:4318", Map.of("FI_ALLOW_INSECURE_HTTP", " 1"), false);
        refused("http://collector.internal:4318", Map.of("FI_ALLOW_INSECURE_HTTP", "0"), false);
    }

    @Test void selfHostedPlaintextIsAllowedWithTheOptInAndWarnsOncePerProcess() {
        assertThat(KeyTransport.check("http://collector.internal:4318", true, OPT_IN, false).plaintext()).isTrue();
        assertThat(KeyTransport.check("http://other.internal", true, NONE, true).plaintext()).isTrue();
        String output = err.toString();
        assertThat(output.split("FI_ALLOW_INSECURE_HTTP", -1)).hasSize(2);
        assertThat(output).doesNotContain("collector.internal").doesNotContain("other.internal");
    }

    @Test void selfHostedTlsNeedsNoOptIn() {
        assertThat(KeyTransport.check("https://collector.internal:4318/base/", true, NONE, false).endpoint("/tracer/v1/traces").toString())
            .isEqualTo("https://collector.internal:4318/base/tracer/v1/traces");
    }

    @Test void anIncompleteKeyPairNeverGoesAnywhere() {
        assertThatThrownBy(() -> KeyTransport.check("https://api.futureagi.com", false, NONE, false)).isInstanceOf(KeyTransport.Refused.class);
        assertThatThrownBy(() -> KeyTransport.check("http://127.0.0.1:1", false, NONE, false)).isInstanceOf(KeyTransport.Refused.class);
    }

    @Test void theEndpointIsOnExactlyTheApprovedHost() {
        assertThat(KeyTransport.check("http://127.0.0.1:4318?x=1#frag", true, NONE, false).endpoint("/tracer/v1/traces").toString())
            .isEqualTo("http://127.0.0.1:4318/tracer/v1/traces");
        assertThat(KeyTransport.check("http://[::1]:4318/", true, NONE, false).endpoint("/t").toString()).isEqualTo("http://[::1]:4318/t");
    }

    @Test void aRedirectStatusIsARefusal() {
        for (int status : new int[] {301, 302, 303, 307, 308})
            assertThatThrownBy(() -> KeyTransport.checkStatus(status)).isInstanceOf(KeyTransport.Refused.class);
        KeyTransport.checkStatus(200); KeyTransport.checkStatus(204); KeyTransport.checkStatus(500);
    }

    /**
     * Loopback is the host as written: these spellings are never loopback, whatever a URL parser makes of them. Without the opt-in each is
     * refused. With it, a spelling this JDK's URL parser reads as a host (2130706433, 0177.0.0.1) is a plaintext non-loopback target, and one
     * it cannot read as a host (127.1, 0x7f.0.0.1, 127.0.0.1.) stays refused as ambiguous. A refusal names no value.
     */
    @ParameterizedTest
    @ValueSource(strings = {"http://127.1:4318", "http://0x7f.0.0.1:4318", "http://2130706433:4318", "http://0177.0.0.1:4318", "http://127.0.0.1.:4318"})
    void numericAndTrailingDotLoopbackSpellingsAreOtherHosts(String url) {
        refused(url);
        refused(url, Map.of("FI_ALLOW_INSECURE_HTTP", "true"), false);
        boolean readAsHost = url.contains("2130706433") || url.contains("0177.0.0.1");
        if (readAsHost) {
            assertThat(KeyTransport.check(url, true, OPT_IN, false).plaintext()).isTrue();
            assertThat(KeyTransport.check(url, true, NONE, true).plaintext()).isTrue();
        } else {
            refused(url, OPT_IN, false);
            refused(url, NONE, true);
        }
    }
}
