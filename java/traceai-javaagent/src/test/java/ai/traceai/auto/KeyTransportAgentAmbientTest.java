package ai.traceai.auto;

import ai.traceai.KeyTransport;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/** The agent refuses, at startup and without installing an exporter, a target or ambient OTLP headers that may not receive the keys. */
class KeyTransportAgentAmbientTest {
    static final String API = "agent-api-value-0001", SECRET = "agent-secret-value-0002";
    PrintStream originalErr; ByteArrayOutputStream err;

    @BeforeEach void setUp() { KeyTransport.resetWarningForTest(); originalErr = System.err; err = new ByteArrayOutputStream(); System.setErr(new PrintStream(err, true)); }
    @AfterEach void tearDown() { System.setErr(originalErr); KeyTransport.resetWarningForTest(); }

    static AutoConfig config(String base, Map<String, Object> extra, Map<String, String> env) {
        Map<String, Object> options = new LinkedHashMap<>(Map.of("api_key", API, "secret_key", SECRET, "base_url", base));
        options.putAll(extra);
        return AutoConfig.resolve(options, env);
    }
    static void refused(AutoConfig config) {
        KeyTransport.Refused refusal = assertThrows(KeyTransport.Refused.class, config::checkKeyTransport);
        for (String value : List.of(API, SECRET)) assertFalse(refusal.getMessage().contains(value), refusal.getMessage());
    }

    @Test void ambientKeyHeaderNamesAndValuesAreRefused() {
        for (String variable : List.of("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS")) {
            for (String value : List.of("X-Api-Key=other", "x-ok=1 , X-Secret-Key = other", "X%2DApi%2DKey=other", "x-trace=" + SECRET, "x-trace=" + API.replace("-", "%2D"), "x-t=prefix-" + API + "-suffix"))
                refused(config("https://api.futureagi.com", Map.of(), Map.of(variable, value)));
        }
    }

    @Test void aKeyValueFromAnIgnoredSourceIsStillJudged() {
        // The environment pair differs from the explicit pair that wins; its values are Future AGI keys too.
        refused(config("https://api.futureagi.com", Map.of(), Map.of("FI_API_KEY", "environment-api-0003", "FI_SECRET_KEY", "environment-secret-0004", "OTEL_EXPORTER_OTLP_HEADERS", "x-trace=environment-secret-0004")));
    }

    @Test void anotherVendorsAmbientHeadersAreAllowed() {
        assertDoesNotThrow(() -> config("https://api.futureagi.com", Map.of(), Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x-vendor=vendor-secret-value-0099,authorization=Bearer%20vendor", "VENDOR_API_TOKEN", "vendor-secret-value-0099")).checkKeyTransport());
    }

    @Test void theTargetIsCheckedAtStartup() {
        refused(config("http://api.futureagi.com", Map.of("allow_insecure_http", "1"), Map.of("FI_ALLOW_INSECURE_HTTP", "1")));
        refused(config("http://collector.internal:4318", Map.of(), Map.of()));
        assertDoesNotThrow(() -> config("http://collector.internal:4318", Map.of("allow_insecure_http", "1"), Map.of()).checkKeyTransport());
        assertDoesNotThrow(() -> config("http://collector.internal:4318", Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", "1")).checkKeyTransport());
    }

    @Test void theOptInIsReadOnlyFromTheEnvironmentAndTheExplicitOption(@org.junit.jupiter.api.io.TempDir java.nio.file.Path directory) throws Exception {
        java.nio.file.Path file = directory.resolve("futureagi.toml");
        java.nio.file.Files.writeString(file, "FI_ALLOW_INSECURE_HTTP = \"1\"\n");
        refused(config("http://collector.internal:4318", Map.of("config_file", file.toString()), Map.of()));
    }

    @Test void aRefusedStartupInstallsNoExporterAndWarnsWithoutValues() {
        TraceAIAuto.resetForTest(); // the agent state is static: start from none, whatever ran before
        assertFalse(TraceAIAuto.runtimeInstalled());
        TraceAIAuto.start(Map.of("api_key", API, "secret_key", SECRET, "base_url", "https://api.futureagi.com"), null, Map.of("OTEL_EXPORTER_OTLP_HEADERS", "X-Api-Key=other"));
        assertFalse(TraceAIAuto.runtimeInstalled(), "an exporter was installed for a refused configuration");
        assertEquals("disabled (key transport refused)", TraceAIAuto.statusOf("wire"), "the startup reached the key-transport gate, so the absence of an exporter is the refusal's");
        for (String value : List.of(API, SECRET, "other")) assertFalse(err.toString().contains(value));
    }

    @Test void numericAndTrailingDotLoopbackSpellingsAreOtherHostsInTheAgentToo() {
        for (String host : List.of("127.1", "0x7f.0.0.1", "2130706433", "0177.0.0.1", "127.0.0.1.")) {
            String base = "http://" + host + ":4318";
            refused(config(base, Map.of(), Map.of()));
            refused(config(base, Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", "true")));
            boolean readAsHost = host.equals("2130706433") || host.equals("0177.0.0.1");
            if (readAsHost) {
                assertDoesNotThrow(() -> config(base, Map.of("allow_insecure_http", "1"), Map.of()).checkKeyTransport(), host);
                assertDoesNotThrow(() -> config(base, Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", "1")).checkKeyTransport(), host);
            } else {
                refused(config(base, Map.of("allow_insecure_http", "1"), Map.of()));
                refused(config(base, Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", "1")));
            }
        }
    }

    /** The environment variable is exactly "1"; the explicit option is a boolean true, or the string "1" or "true". */
    @Test void theExplicitOptInAcceptsTrueOneAndTheStringTrueAndNothingElse() {
        String base = "http://collector.internal:4318";
        for (Object accepted : List.of(Boolean.TRUE, "1", "true"))
            assertDoesNotThrow(() -> config(base, Map.of("allow_insecure_http", accepted), Map.of()).checkKeyTransport(), String.valueOf(accepted));
        for (Object rejected : List.of(Boolean.FALSE, "0", "false", "yes", "", "2"))
            refused(config(base, Map.of("allow_insecure_http", rejected), Map.of()));
        for (String value : List.of("true", "TRUE", "yes", "0", ""))
            refused(config(base, Map.of(), Map.of("FI_ALLOW_INSECURE_HTTP", value)));
    }

    @Test void controlTheSameStartupWithoutARefusalInstallsAnExporter() {
        try {
            TraceAIAuto.resetForTest();
            assertFalse(TraceAIAuto.runtimeInstalled(), "the state is reset before the case");
            TraceAIAuto.start(Map.of("api_key", API, "secret_key", SECRET, "base_url", "https://api.futureagi.com"), noInstrumentation(), Map.of());
            assertTrue(TraceAIAuto.runtimeInstalled(), "without a refusal the same startup installs its exporter, so the refused case is not vacuous");
        } finally { TraceAIAuto.resetForTest(); }
    }

    /** An Instrumentation that accepts transformers and does nothing, so the control starts the agent without attaching to this JVM. */
    static java.lang.instrument.Instrumentation noInstrumentation() {
        return (java.lang.instrument.Instrumentation)java.lang.reflect.Proxy.newProxyInstance(KeyTransportAgentAmbientTest.class.getClassLoader(), new Class<?>[] {java.lang.instrument.Instrumentation.class},
            (proxy, method, arguments) -> {
                Class<?> type = method.getReturnType();
                if (type == boolean.class) return method.getName().equals("isRetransformClassesSupported") || method.getName().equals("isModifiableClass");
                if (type == Class[].class) return new Class<?>[0];
                if (type == long.class) return 0L;
                return null;
            });
    }
}
