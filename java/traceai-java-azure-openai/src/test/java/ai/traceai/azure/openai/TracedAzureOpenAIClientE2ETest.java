package ai.traceai.azure.openai;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.ai.openai.models.*;
import com.azure.core.credential.AzureKeyCredential;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.List;

/**
 * E2E tests for TracedAzureOpenAIClient.
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy credentials) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-azure-openai-e2e)</li>
 *   <li>AZURE_OPENAI_ENDPOINT - Azure OpenAI endpoint URL</li>
 *   <li>AZURE_OPENAI_API_KEY - Azure OpenAI API key</li>
 *   <li>AZURE_OPENAI_DEPLOYMENT - Azure OpenAI deployment name (default: gpt-4o-mini)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedAzureOpenAIClientE2ETest {

    private static FITracer tracer;
    private static TracedAzureOpenAIClient tracedClient;
    private static String deploymentName;

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
                    : "java-azure-openai-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String endpoint = System.getenv("AZURE_OPENAI_ENDPOINT") != null
            ? System.getenv("AZURE_OPENAI_ENDPOINT")
            : "https://dummy.openai.azure.com/";

        String apiKey = System.getenv("AZURE_OPENAI_API_KEY") != null
            ? System.getenv("AZURE_OPENAI_API_KEY")
            : "dummy-key-for-e2e";

        deploymentName = System.getenv("AZURE_OPENAI_DEPLOYMENT") != null
            ? System.getenv("AZURE_OPENAI_DEPLOYMENT")
            : "gpt-4o-mini";

        OpenAIClient client = new OpenAIClientBuilder()
            .endpoint(endpoint)
            .credential(new AzureKeyCredential(apiKey))
            .buildClient();

        tracedClient = new TracedAzureOpenAIClient(client, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportChatCompletionsSpan() {
        ChatCompletionsOptions options = new ChatCompletionsOptions(List.of(
            new ChatRequestUserMessage("Say 'Hello from Java E2E test' and nothing else.")
        ));
        options.setMaxTokens(50);

        try {
            ChatCompletions result = tracedClient.getChatCompletions(deploymentName, options);
            System.out.println("[E2E] Azure OpenAI chat response: " +
                result.getChoices().get(0).getMessage().getContent());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportEmbeddingsSpan() {
        EmbeddingsOptions options = new EmbeddingsOptions(
            List.of("Hello from Java E2E test")
        );

        try {
            Embeddings result = tracedClient.getEmbeddings("text-embedding-ada-002", options);
            System.out.println("[E2E] Azure OpenAI embedding dimensions: " +
                result.getData().get(0).getEmbedding().size());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportCompletionsSpan() {
        CompletionsOptions options = new CompletionsOptions(
            List.of("The capital of France is")
        );
        options.setMaxTokens(20);

        try {
            Completions result = tracedClient.getCompletions(deploymentName, options);
            System.out.println("[E2E] Azure OpenAI completions response: " +
                result.getChoices().get(0).getText());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
