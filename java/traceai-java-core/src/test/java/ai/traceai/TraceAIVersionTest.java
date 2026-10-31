package ai.traceai;

import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.util.Properties;

import static org.assertj.core.api.Assertions.assertThat;

class TraceAIVersionTest {

    @Test
    void versionComesFromTheMavenFilteredResource() throws Exception {
        Properties properties = new Properties();
        try (InputStream stream = TraceAI.class.getResourceAsStream("/traceai-version.properties")) {
            assertThat(stream).isNotNull();
            properties.load(stream);
        }
        assertThat(TraceAI.getVersion())
            .isEqualTo(properties.getProperty("version"))
            .isNotEqualTo("unknown")
            .doesNotContain("${");
    }
}
