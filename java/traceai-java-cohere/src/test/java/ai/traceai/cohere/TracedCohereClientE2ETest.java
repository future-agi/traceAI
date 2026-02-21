package ai.traceai.cohere;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import com.cohere.api.Cohere;
import com.cohere.api.requests.*;
import com.cohere.api.types.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.List;

/**
 * E2E tests for TracedCohereClient.
 *
 * <p>These tests make REAL API calls and verify that spans are exported
 * to the FI backend for visual verification. They are separate from the
 * mock-based unit tests in TracedCohereClientTest.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>COHERE_API_KEY - API key for Cohere</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-cohere-e2e)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedCohereClientE2ETest {

    private static FITracer tracer;
    private static TracedCohereClient tracedClient;

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
                    : "java-cohere-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String cohereApiKey = System.getenv("COHERE_API_KEY");
        Cohere client = Cohere.builder()
            .token(cohereApiKey != null ? cohereApiKey : "dummy-key")
            .build();

        tracedClient = new TracedCohereClient(client, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000); // Allow batch export to flush
    }

    @Test
    @Order(1)
    @EnabledIfEnvironmentVariable(named = "COHERE_API_KEY", matches = ".+")
    void shouldExportChatSpan() {
        ChatRequest request = ChatRequest.builder()
            .message("Say 'Hello from Java E2E test' and nothing else.")
            .model("command-r")
            .maxTokens(50)
            .build();

        assertThatCode(() -> {
            NonStreamedChatResponse response = tracedClient.chat(request);
            assertThat(response).isNotNull();
            assertThat(response.getText()).isNotEmpty();
            System.out.println("[E2E] Cohere chat response: " + response.getText());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(2)
    @EnabledIfEnvironmentVariable(named = "COHERE_API_KEY", matches = ".+")
    void shouldExportChatWithTemperatureSpan() {
        ChatRequest request = ChatRequest.builder()
            .message("What is 2 + 2? Reply with just the number.")
            .model("command-r")
            .temperature(0.0f)
            .maxTokens(10)
            .build();

        assertThatCode(() -> {
            NonStreamedChatResponse response = tracedClient.chat(request);
            assertThat(response).isNotNull();
            System.out.println("[E2E] Cohere deterministic chat response: " + response.getText());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(3)
    @EnabledIfEnvironmentVariable(named = "COHERE_API_KEY", matches = ".+")
    void shouldExportEmbedSpan() {
        EmbedRequest request = EmbedRequest.builder()
            .texts(List.of("Hello from Java E2E test", "Second text for embedding"))
            .model("embed-english-v3.0")
            .inputType(EmbedInputType.SEARCH_DOCUMENT)
            .build();

        assertThatCode(() -> {
            EmbedResponse response = tracedClient.embed(request);
            assertThat(response).isNotNull();
            System.out.println("[E2E] Cohere embed response received");
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(4)
    @EnabledIfEnvironmentVariable(named = "COHERE_API_KEY", matches = ".+")
    void shouldExportRerankSpan() {
        RerankRequest request = RerankRequest.builder()
            .query("What is the capital of France?")
            .documents(List.of(
                RerankRequestDocumentsItem.of("Paris is the capital of France."),
                RerankRequestDocumentsItem.of("Berlin is the capital of Germany."),
                RerankRequestDocumentsItem.of("Madrid is the capital of Spain.")
            ))
            .model("rerank-english-v3.0")
            .topN(2)
            .build();

        assertThatCode(() -> {
            RerankResponse response = tracedClient.rerank(request);
            assertThat(response).isNotNull();
            assertThat(response.getResults()).isNotEmpty();
            System.out.println("[E2E] Cohere rerank top result index: " +
                response.getResults().get(0).getIndex());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(5)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
