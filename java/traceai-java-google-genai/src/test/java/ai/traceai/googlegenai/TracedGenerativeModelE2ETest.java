package ai.traceai.googlegenai;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

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
 * <p>These tests make REAL API calls and verify that spans are exported
 * to the FI backend for visual verification. They are separate from the
 * mock-based unit tests in TracedGenerativeModelTest.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>GOOGLE_API_KEY - API key for Google GenAI (Gemini)</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-google-genai-e2e)</li>
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
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                    ? System.getenv("FI_PROJECT_NAME")
                    : "java-google-genai-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String googleApiKey = System.getenv("GOOGLE_API_KEY");
        Client client = Client.builder()
            .apiKey(googleApiKey != null ? googleApiKey : "dummy-key")
            .build();

        tracedModel = new TracedGenerativeModel(client, tracer, MODEL_NAME);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000); // Allow batch export to flush
    }

    @Test
    @Order(1)
    @EnabledIfEnvironmentVariable(named = "GOOGLE_API_KEY", matches = ".+")
    void shouldExportGenerateContentSpan() {
        assertThatCode(() -> {
            GenerateContentResponse response = tracedModel.generateContent(
                "Say 'Hello from Java E2E test' and nothing else.");
            assertThat(response).isNotNull();
            String text = response.text();
            assertThat(text).isNotEmpty();
            System.out.println("[E2E] Google GenAI response: " + text);
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(2)
    @EnabledIfEnvironmentVariable(named = "GOOGLE_API_KEY", matches = ".+")
    void shouldExportCountTokensSpan() {
        assertThatCode(() -> {
            CountTokensResponse response = tracedModel.countTokens(
                "Hello from Java E2E test, count my tokens please.");
            assertThat(response).isNotNull();
            response.totalTokens().ifPresent(total -> {
                assertThat(total).isGreaterThan(0);
                System.out.println("[E2E] Google GenAI token count: " + total);
            });
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(3)
    @EnabledIfEnvironmentVariable(named = "GOOGLE_API_KEY", matches = ".+")
    void shouldExportChatSessionSpan() {
        assertThatCode(() -> {
            TracedGenerativeModel.TracedChat chat = tracedModel.startChat();
            assertThat(chat).isNotNull();

            GenerateContentResponse response1 = chat.sendMessage("My name is Java E2E Tester.");
            assertThat(response1).isNotNull();
            System.out.println("[E2E] Chat turn 1: " + response1.text());

            GenerateContentResponse response2 = chat.sendMessage("What is my name?");
            assertThat(response2).isNotNull();
            System.out.println("[E2E] Chat turn 2: " + response2.text());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(4)
    void shouldCreateTracedModelSuccessfully() {
        assertThat(tracedModel).isNotNull();
        assertThat(tracedModel.unwrap()).isNotNull();
        assertThat(tracedModel.getModelName()).isEqualTo(MODEL_NAME);
    }
}
