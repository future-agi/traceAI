package ai.traceai.vertexai;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import com.google.cloud.vertexai.VertexAI;
import com.google.cloud.vertexai.api.CountTokensResponse;
import com.google.cloud.vertexai.api.GenerateContentResponse;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * E2E tests for TracedGenerativeModel (Vertex AI).
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy project IDs) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-vertexai-e2e)</li>
 *   <li>GOOGLE_CLOUD_PROJECT - Google Cloud project ID</li>
 *   <li>GOOGLE_CLOUD_LOCATION - GCP region (default: us-central1)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedGenerativeModelE2ETest {

    private static FITracer tracer;
    private static TracedGenerativeModel tracedModel;
    private static VertexAI vertexAI;
    private static final String MODEL_NAME = "gemini-2.0-flash";

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
                    : "java-vertexai-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String projectId = System.getenv("GOOGLE_CLOUD_PROJECT") != null
            ? System.getenv("GOOGLE_CLOUD_PROJECT")
            : "dummy-project-for-e2e";

        String location = System.getenv("GOOGLE_CLOUD_LOCATION") != null
            ? System.getenv("GOOGLE_CLOUD_LOCATION")
            : "us-central1";

        vertexAI = new VertexAI(projectId, location);
        GenerativeModel model = new GenerativeModel(MODEL_NAME, vertexAI);
        tracedModel = new TracedGenerativeModel(model, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
        if (vertexAI != null) {
            vertexAI.close();
        }
    }

    @Test
    @Order(1)
    void shouldExportGenerateContentSpan() {
        try {
            GenerateContentResponse response = tracedModel.generateContent(
                "Say 'Hello from Java E2E test' and nothing else.");
            String text = response.getCandidatesList().get(0)
                .getContent().getPartsList().get(0).getText();
            System.out.println("[E2E] Vertex AI response: " + text);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportCountTokensSpan() {
        try {
            CountTokensResponse response = tracedModel.countTokens(
                "Hello from Java E2E test, count my tokens please.");
            System.out.println("[E2E] Vertex AI token count: " + response.getTotalTokens());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldCreateTracedModelSuccessfully() {
        assertThat(tracedModel).isNotNull();
        assertThat(tracedModel.unwrap()).isNotNull();
    }
}
