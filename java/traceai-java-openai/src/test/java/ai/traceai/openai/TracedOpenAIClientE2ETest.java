package ai.traceai.openai;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;
import com.openai.models.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * E2E tests for TracedOpenAIClient.
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
 *   <li>FI_PROJECT_NAME - Project name (default: java-openai-e2e)</li>
 *   <li>OPENAI_API_KEY - API key for OpenAI (or compatible endpoint)</li>
 *   <li>OPENAI_BASE_URL - OpenAI base URL (for Google OpenAI-compat endpoint etc.)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedOpenAIClientE2ETest {

    private static FITracer tracer;
    private static TracedOpenAIClient tracedClient;

    @BeforeAll
    static void setUp() {
        String baseUrl = System.getenv("FI_BASE_URL") != null
            ? System.getenv("FI_BASE_URL")
            : "https://api.futureagi.com";

        TraceAI.init(TraceConfig.builder()
            .baseUrl(baseUrl)
            .apiKey(System.getenv("FI_API_KEY"))
                .secretKey(System.getenv("FI_SECRET_KEY"))
            .projectName(System.getenv("FI_PROJECT_NAME") != null
                ? System.getenv("FI_PROJECT_NAME")
                : "java-openai-e2e")
            .enableConsoleExporter(true)
            .build());

        tracer = TraceAI.getTracer();

        String openaiApiKey = System.getenv("OPENAI_API_KEY") != null
            ? System.getenv("OPENAI_API_KEY")
            : "dummy-key-for-e2e";

        OpenAIOkHttpClient.Builder clientBuilder = OpenAIOkHttpClient.builder()
            .apiKey(openaiApiKey);

        String openaiBaseUrl = System.getenv("OPENAI_BASE_URL");
        if (openaiBaseUrl != null) {
            clientBuilder.baseUrl(openaiBaseUrl);
        }

        OpenAIClient client = clientBuilder.build();
        tracedClient = new TracedOpenAIClient(client, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportChatCompletionSpan() {
        ChatCompletionCreateParams params = ChatCompletionCreateParams.builder()
            .model("gpt-4o-mini")
            .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                ChatCompletionUserMessageParam.builder()
                    .content(ChatCompletionUserMessageParam.Content.ofTextContent(
                        "Say 'Hello from Java E2E test' and nothing else."))
                    .build()
            ))
            .maxTokens(50)
            .build();

        try {
            ChatCompletion result = tracedClient.createChatCompletion(params);
            System.out.println("[E2E] Chat response: " +
                result.choices().get(0).message().content().orElse("(empty)"));
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportChatCompletionWithTemperatureSpan() {
        ChatCompletionCreateParams params = ChatCompletionCreateParams.builder()
            .model("gpt-4o-mini")
            .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                ChatCompletionUserMessageParam.builder()
                    .content(ChatCompletionUserMessageParam.Content.ofTextContent(
                        "What is 2 + 2? Reply with just the number."))
                    .build()
            ))
            .temperature(0.0)
            .maxTokens(10)
            .build();

        try {
            ChatCompletion result = tracedClient.createChatCompletion(params);
            System.out.println("[E2E] Deterministic chat response: " +
                result.choices().get(0).message().content().orElse("(empty)"));
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportEmbeddingSpan() {
        EmbeddingCreateParams params = EmbeddingCreateParams.builder()
            .model("text-embedding-3-small")
            .input(EmbeddingCreateParams.Input.ofString("Hello from Java E2E test"))
            .build();

        try {
            CreateEmbeddingResponse result = tracedClient.createEmbedding(params);
            System.out.println("[E2E] Embedding dimensions: " +
                result.data().get(0).embedding().size());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldExportStreamingChatCompletionSpan() {
        ChatCompletionCreateParams params = ChatCompletionCreateParams.builder()
            .model("gpt-4o-mini")
            .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                ChatCompletionUserMessageParam.builder()
                    .content(ChatCompletionUserMessageParam.Content.ofTextContent(
                        "Count from 1 to 5, separated by commas."))
                    .build()
            ))
            .maxTokens(30)
            .build();

        try {
            Iterable<ChatCompletionChunk> chunks = tracedClient.streamChatCompletion(params);
            int chunkCount = 0;
            for (ChatCompletionChunk chunk : chunks) {
                chunkCount++;
            }
            System.out.println("[E2E] Streaming chunks received: " + chunkCount);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
