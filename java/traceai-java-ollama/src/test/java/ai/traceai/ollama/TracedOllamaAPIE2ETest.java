package ai.traceai.ollama;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import io.github.ollama4j.OllamaAPI;
import io.github.ollama4j.models.OllamaResult;
import io.github.ollama4j.models.chat.OllamaChatMessage;
import io.github.ollama4j.models.chat.OllamaChatMessageRole;
import io.github.ollama4j.models.chat.OllamaChatResult;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.List;

/**
 * E2E tests for TracedOllamaAPI.
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * connection failures) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-ollama-e2e)</li>
 *   <li>OLLAMA_HOST - Ollama host URL (default: http://localhost:11434)</li>
 *   <li>OLLAMA_MODEL - Model name (default: llama3.2:1b)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedOllamaAPIE2ETest {

    private static FITracer tracer;
    private static TracedOllamaAPI tracedApi;
    private static String modelName;

    @BeforeAll
    static void setUp() {
        String baseUrl = System.getenv("FI_BASE_URL") != null
            ? System.getenv("FI_BASE_URL")
            : "https://api.futureagi.com";

        if (!TraceAI.isInitialized()) {
            TraceAI.init(TraceConfig.builder()
                .baseUrl(baseUrl)
                .apiKey(System.getenv("FI_API_KEY"))
                .secretKey(System.getenv("FI_SECRET_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                    ? System.getenv("FI_PROJECT_NAME")
                    : "java-ollama-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String host = System.getenv("OLLAMA_HOST") != null
            ? System.getenv("OLLAMA_HOST")
            : "http://localhost:11434";

        OllamaAPI api = new OllamaAPI(host);
        api.setRequestTimeoutSeconds(60);

        tracedApi = new TracedOllamaAPI(api, tracer);

        modelName = System.getenv("OLLAMA_MODEL") != null
            ? System.getenv("OLLAMA_MODEL")
            : "llama3.2:1b";
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportGenerateSpan() {
        try {
            OllamaResult result = tracedApi.generate(modelName,
                "Say 'Hello from Java E2E test' and nothing else.");
            System.out.println("[E2E] Ollama generate response: " + result.getResponse());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportChatSpan() {
        try {
            List<OllamaChatMessage> messages = List.of(
                new OllamaChatMessage(OllamaChatMessageRole.USER,
                    "What is 2 + 2? Reply with just the number.")
            );

            OllamaChatResult result = tracedApi.chat(modelName, messages);
            System.out.println("[E2E] Ollama chat response: " + result.getResponse());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportEmbedSpan() {
        try {
            List<Double> embedding = tracedApi.embed(modelName,
                "Hello from Java E2E test");
            System.out.println("[E2E] Ollama embedding dimensions: " + embedding.size());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldExportListModelsSpan() {
        try {
            List<String> models = tracedApi.listModels();
            System.out.println("[E2E] Ollama models available: " + models);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldCreateTracedApiSuccessfully() {
        assertThat(tracedApi).isNotNull();
        assertThat(tracedApi.unwrap()).isNotNull();
    }
}
