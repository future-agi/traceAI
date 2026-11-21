package ai.traceai;

import org.junit.jupiter.api.Test;

import java.util.AbstractMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/** With export off nothing about the target or the keys is read from the environment, and no exporter exists. */
class TraceConfigExportSwitchTest {
    /** An environment that counts every read. */
    private static final class CountingEnvironment extends AbstractMap<String, String> {
        final AtomicInteger reads = new AtomicInteger();
        private final Map<String, String> values = Map.of("FI_BASE_URL", "http://127.0.0.1:4318", "FI_API_KEY", "environment-api-key", "FI_SECRET_KEY", "environment-secret-key");
        @Override public String get(Object key) { reads.incrementAndGet(); return values.get(key); }
        @Override public boolean containsKey(Object key) { reads.incrementAndGet(); return values.containsKey(key); }
        @Override public String getOrDefault(Object key, String fallback) { reads.incrementAndGet(); return values.getOrDefault(key, fallback); }
        @Override public Set<Entry<String, String>> entrySet() { reads.incrementAndGet(); return values.entrySet(); }
    }

    @Test void aBuilderWithExportOffReadsNeitherTheBaseUrlNorTheKeys() {
        CountingEnvironment env = new CountingEnvironment();
        TraceConfig config = TraceConfig.builder().exportEnabled(false).baseUrl("http://ignored").apiKey("ignored").secretKey("ignored").build(env::get);
        assertThat(config.isExportEnabled()).isFalse();
        assertThat(config.getBaseUrl()).isNull();
        assertThat(config.getApiKey()).isNull();
        assertThat(config.getSecretKey()).isNull();
        assertThat(env.reads).hasValue(0);
    }

    @Test void theDefaultStillFillsUnsetFieldsFromTheEnvironment() {
        CountingEnvironment env = new CountingEnvironment();
        TraceConfig config = TraceConfig.builder().build(env::get);
        assertThat(config.isExportEnabled()).isTrue();
        assertThat(config.getBaseUrl()).isEqualTo("http://127.0.0.1:4318");
        assertThat(config.getApiKey()).isEqualTo("environment-api-key");
        assertThat(env.reads.get()).isGreaterThan(0);
    }

    @Test void initWithExportOffNeverReadsTheEnvironmentAndCreatesNoExporter() {
        CountingEnvironment env = new CountingEnvironment();
        TraceAI.shutdown();
        try {
            TraceConfig config = TraceConfig.builder().exportEnabled(false).projectName("p").build(env::get);
            TraceAI.initWithEnvironment(config, env);
            assertThat(TraceAI.isInitialized()).isTrue();
            assertThat(env.reads).as("the environment was read after export was switched off").hasValue(0);
        } finally { TraceAI.shutdown(); }
    }
}
