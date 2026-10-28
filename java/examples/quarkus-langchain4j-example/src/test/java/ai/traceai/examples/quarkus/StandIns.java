package ai.traceai.examples.quarkus;

import io.quarkus.test.common.QuarkusTestResourceLifecycleManager;

import java.util.Map;

/**
 * Starts the scripted model server and the OTLP recorder and points the application at them through the same
 * variables a customer sets (OPENAI_*, FI_*). Nothing in the tests talks to a real provider or to Future AGI.
 */
public class StandIns implements QuarkusTestResourceLifecycleManager {

    static final String PROVIDER_KEY = "sk-provider-key-must-not-be-exported-4d1f";
    static final String FI_API_KEY = "fi-api-key-test-9b2e";
    static final String FI_SECRET_KEY = "fi-secret-key-test-51ac";
    static final String PROJECT = "quarkus-langchain4j-test";
    static final String MODEL = "gpt-test";

    static volatile MockOpenAi model;
    static volatile OtlpRecorder collector;

    @Override
    public Map<String, String> start() {
        try {
            model = new MockOpenAi();
            collector = new OtlpRecorder();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
        return Map.of(
                "OPENAI_API_KEY", PROVIDER_KEY,
                "OPENAI_BASE_URL", model.baseUrl(),
                "OPENAI_MODEL", MODEL,
                "FI_BASE_URL", collector.baseUrl(),
                "FI_API_KEY", FI_API_KEY,
                "FI_SECRET_KEY", FI_SECRET_KEY,
                "FI_PROJECT_NAME", PROJECT);
    }

    @Override
    public void stop() {
        if (model != null) {
            model.close();
        }
        if (collector != null) {
            collector.close();
        }
    }
}
