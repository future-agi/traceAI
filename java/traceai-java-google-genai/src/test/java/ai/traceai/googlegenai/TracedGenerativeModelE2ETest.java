package ai.traceai.googlegenai;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import com.google.genai.Client;
import com.google.genai.types.CountTokensResponse;
import com.google.genai.types.GenerateContentResponse;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * E2E tests for TracedGenerativeModel (Google GenAI / Gemini).
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy keys) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-google-genai-e2e)</li>
 *   <li>GOOGLE_API_KEY - API key for Google GenAI (Gemini)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedGenerativeModelE2ETest {

    private static FITracer tracer;
    private static TracedGenerativeModel tracedModel;
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
                    : "java-google-genai-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String googleApiKey = System.getenv("GOOGLE_API_KEY") != null
            ? System.getenv("GOOGLE_API_KEY")
            : "dummy-key-for-e2e";

        Client client = Client.builder()
            .apiKey(googleApiKey)
            .build();

        tracedModel = new TracedGenerativeModel(client, tracer, MODEL_NAME);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportGenerateContentSpan() {
        try {
            GenerateContentResponse response = tracedModel.generateContent(
                "Say 'Hello from Java E2E test' and nothing else.");
            String text = response.text();
            System.out.println("[E2E] Google GenAI response: " + text);
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
            response.totalTokens().ifPresent(total ->
                System.out.println("[E2E] Google GenAI token count: " + total));
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportChatSessionSpan() {
        try {
            TracedGenerativeModel.TracedChat chat = tracedModel.startChat();

            GenerateContentResponse response1 = chat.sendMessage("My name is Java E2E Tester.");
            System.out.println("[E2E] Chat turn 1: " + response1.text());

            GenerateContentResponse response2 = chat.sendMessage("What is my name?");
            System.out.println("[E2E] Chat turn 2: " + response2.text());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldCreateTracedModelSuccessfully() {
        assertThat(tracedModel).isNotNull();
        assertThat(tracedModel.unwrap()).isNotNull();
        assertThat(tracedModel.getModelName()).isEqualTo(MODEL_NAME);
    }
}
