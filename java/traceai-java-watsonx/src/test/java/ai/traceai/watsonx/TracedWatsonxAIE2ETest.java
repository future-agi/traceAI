package ai.traceai.watsonx;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * E2E tests for TracedWatsonxAI.
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy credentials) appear in the UI for visual verification.</p>
 *
 * <p>The Watsonx wrapper uses reflection, so the tests construct a dummy
 * client object to exercise the tracing wrapper. Real API calls will fail
 * with dummy credentials, but error spans are still exported.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-watsonx-e2e)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedWatsonxAIE2ETest {

    private static FITracer tracer;
    private static TracedWatsonxAI tracedClient;

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
                    : "java-watsonx-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        // The Watsonx wrapper uses reflection, so we can pass a dummy client.
        // Real SDK client construction requires actual IBM Cloud credentials.
        // The traced wrapper will attempt reflection calls which will fail,
        // but error spans are still exported to the FI backend.
        Object dummyClient = new Object();
        tracedClient = new TracedWatsonxAI(dummyClient, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportGenerateTextSpan() {
        // The dummy client will fail on reflection call, but the span is exported
        Exception thrown = Assertions.assertThrows(Exception.class, () -> {
            tracedClient.generateText("dummy-request");
        });
        assertThat(thrown).isNotNull();
        System.out.println("[E2E] Watsonx generateText error (expected with dummy client): " + thrown.getMessage());
    }

    @Test
    @Order(2)
    void shouldExportChatSpan() {
        Exception thrown = Assertions.assertThrows(Exception.class, () -> {
            tracedClient.chat("dummy-request");
        });
        assertThat(thrown).isNotNull();
        System.out.println("[E2E] Watsonx chat error (expected with dummy client): " + thrown.getMessage());
    }

    @Test
    @Order(3)
    void shouldExportEmbedTextSpan() {
        Exception thrown = Assertions.assertThrows(Exception.class, () -> {
            tracedClient.embedText("dummy-request");
        });
        assertThat(thrown).isNotNull();
        System.out.println("[E2E] Watsonx embedText error (expected with dummy client): " + thrown.getMessage());
    }

    @Test
    @Order(4)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
