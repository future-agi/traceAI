package ai.traceai;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Ambient OTLP headers that name a Future AGI key header or carry a Future AGI key value refuse the configuration. */
class KeyTransportAmbientTest {
    private static final String API = "synthetic-api-value-0001", SECRET = "synthetic-secret-value-0002";
    private static final List<String> KEYS = List.of(API, SECRET);
    private PrintStream originalErr;
    private ByteArrayOutputStream err;

    @BeforeEach void capture() { KeyTransport.resetWarningForTest(); originalErr = System.err; err = new ByteArrayOutputStream(); System.setErr(new PrintStream(err, true)); }
    @AfterEach void restore() { System.setErr(originalErr); KeyTransport.resetWarningForTest(); }

    private static void refused(String variable, String value) {
        assertThatThrownBy(() -> KeyTransport.checkAmbientHeaders(Map.of(variable, value), KEYS))
            .isInstanceOf(KeyTransport.Refused.class)
            .hasMessageNotContaining(API).hasMessageNotContaining(SECRET).hasMessageNotContaining(value);
    }

    @ParameterizedTest
    @ValueSource(strings = {"X-Api-Key=other", "x-secret-key=other", "X-API-KEY=other", "authorization=Bearer%20abc, x-secret-key=other", "  X-Api-Key  =other", "X%2DApi%2DKey=other",
        "x-ok=1,x-api-key=other", "x-ok=1 , \tX-Secret-Key = other"})
    void aKeyHeaderNameInEitherVariableIsRefused(String value) {
        refused("OTEL_EXPORTER_OTLP_HEADERS", value);
        refused("OTEL_EXPORTER_OTLP_TRACES_HEADERS", value);
    }

    @Test void aKeyValueUnderAnUnrelatedNameIsRefusedPlainOrPercentEncoded() {
        refused("OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=" + SECRET);
        refused("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-trace-token=prefix-" + API + "-suffix");
        refused("OTEL_EXPORTER_OTLP_HEADERS", "x-trace-token=" + SECRET.replace("-", "%2D"));
        refused("OTEL_EXPORTER_OTLP_HEADERS", "x-ok=1 , x-trace-token = " + API);
    }

    @Test void anotherVendorsKeysAndOtherCredentialVariablesAreAllowed() {
        assertThatCode(() -> KeyTransport.checkAmbientHeaders(Map.of(
            "OTEL_EXPORTER_OTLP_HEADERS", "x-vendor-token=vendor-secret-value-0099,authorization=Bearer%20vendor-0099",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-other=short", "VENDOR_API_TOKEN", "vendor-secret-value-0099"), KEYS)).doesNotThrowAnyException();
        assertThatCode(() -> KeyTransport.checkAmbientHeaders(Map.of(), KEYS)).doesNotThrowAnyException();
    }

    @Test void anUnparseableVariableFailsClosedOnlyWhenKeysAreConfigured() {
        for (String broken : new String[] {"no-equals-sign", "x-a=%zz", "=value", "x-a=1,,x-b"}) {
            assertThatCode(() -> KeyTransport.checkAmbientHeaders(Map.of("OTEL_EXPORTER_OTLP_HEADERS", broken), List.of())).doesNotThrowAnyException();
            assertThatThrownBy(() -> KeyTransport.checkAmbientHeaders(Map.of("OTEL_EXPORTER_OTLP_HEADERS", broken), KEYS)).isInstanceOf(KeyTransport.Refused.class);
        }
    }

    @Test void initRefusesBeforeAnyStateIsCreated() {
        TraceConfig config = TraceConfig.builder().baseUrl("https://api.futureagi.com").apiKey(API).secretKey(SECRET).projectName("p").exportIntervalMs(60000).build();
        TraceAI.shutdown();
        for (Map<String, String> env : List.of(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "X-Api-Key=other"), Map.of("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-trace=" + SECRET))) {
            assertThatThrownBy(() -> TraceAI.initWithEnvironment(config, env)).isInstanceOf(KeyTransport.Refused.class).hasMessageNotContaining(SECRET);
            assertThat(TraceAI.isInitialized()).isFalse();
        }
        TraceAI.initWithEnvironment(config, Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x-vendor=vendor-secret-value-0099"));
        assertThat(TraceAI.isInitialized()).isTrue();
        TraceAI.shutdown();
    }

    @Test void zoneLiteralsAreNeverLoopbackAndNeedTheOptInForPlaintext() {
        assertThat(KeyTransport.check("https://[fe80::1%25en0]:4318", true, Map.of(), false).plaintext()).isFalse();
        assertThatThrownBy(() -> KeyTransport.check("http://[fe80::1%25eth0]:4318", true, Map.of(), false)).isInstanceOf(KeyTransport.Refused.class)
            .hasMessageContaining("FI_ALLOW_INSECURE_HTTP");
        KeyTransport.Target target = KeyTransport.check("http://[fe80::1%25eth0]:4318", true, Map.of("FI_ALLOW_INSECURE_HTTP", "1"), false);
        assertThat(target.plaintext()).isTrue();
        assertThat(err.toString().split("FI_ALLOW_INSECURE_HTTP", -1)).hasSize(2);
        assertThat(target.endpoint("/x").toString()).startsWith("http://[fe80::1%25eth0]:4318");
        // Anything else about a zone stays ambiguous.
        for (String url : new String[] {"https://[fe80::1%eth0]:4318", "https://[fe80::1%41]:4318", "https://[fe80::1%25]:4318", "https://[fe80::1%25e/h]:4318", "https://fe80::1%25eth0:4318"})
            assertThatThrownBy(() -> KeyTransport.check(url, true, Map.of(), false)).isInstanceOf(KeyTransport.Refused.class);
    }

    @ParameterizedTest
    @ValueSource(strings = {"http://localhost/@evil.example", "http://localhost/%5C@evil.example", "http://localhost?@evil", "http://localhost/a%5cb", "http://localhost/a\\b", "http://localhost/x?y=@z",
        "https://api.futureagi.com/%5C", "https://api.futureagi.com/a@b"})
    void anAtSignOrBackslashAnywhereInThePathOrQueryIsRefused(String url) {
        assertThatThrownBy(() -> KeyTransport.check(url, true, Map.of(), false)).isInstanceOf(KeyTransport.Refused.class).hasMessageNotContaining("evil");
    }
}
