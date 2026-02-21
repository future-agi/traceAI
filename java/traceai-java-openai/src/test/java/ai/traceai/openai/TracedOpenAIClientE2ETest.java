package ai.traceai.openai;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

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
 * <p>These tests make REAL API calls and verify that spans are exported
 * to the FI backend for visual verification. They are separate from the
 * mock-based unit tests in TracedOpenAIClientTest.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>OPENAI_API_KEY - API key for OpenAI (or compatible endpoint)</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-openai-e2e)</li>
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
            .projectName(System.getenv("FI_PROJECT_NAME") != null
                ? System.getenv("FI_PROJECT_NAME")
                : "java-openai-e2e")
            .enableConsoleExporter(true)
            .build());

        tracer = TraceAI.getTracer();

        // Build OpenAI client — supports custom base URL for OpenAI-compat endpoints
        OpenAIOkHttpClient.Builder clientBuilder = OpenAIOkHttpClient.builder();

        String openaiApiKey = System.getenv("OPENAI_API_KEY");
        if (openaiApiKey != null) {
            clientBuilder.apiKey(openaiApiKey);
        }

        String openaiBaseUrl = System.getenv("OPENAI_BASE_URL");
        if (openaiBaseUrl != null) {
            clientBuilder.baseUrl(openaiBaseUrl);
        }

        OpenAIClient client = clientBuilder.build();
        tracedClient = new TracedOpenAIClient(client, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000); // Allow batch export to flush
    }

    @Test
    @Order(1)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
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

        assertThatCode(() -> {
            ChatCompletion result = tracedClient.createChatCompletion(params);
            assertThat(result).isNotNull();
            assertThat(result.choices()).isNotEmpty();
            assertThat(result.choices().get(0).message().content()).isPresent();
            System.out.println("[E2E] Chat response: " +
                result.choices().get(0).message().content().orElse("(empty)"));
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(2)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
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

        assertThatCode(() -> {
            ChatCompletion result = tracedClient.createChatCompletion(params);
            assertThat(result).isNotNull();
            System.out.println("[E2E] Deterministic chat response: " +
                result.choices().get(0).message().content().orElse("(empty)"));
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(3)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
    void shouldExportEmbeddingSpan() {
        EmbeddingCreateParams params = EmbeddingCreateParams.builder()
            .model("text-embedding-3-small")
            .input(EmbeddingCreateParams.Input.ofString("Hello from Java E2E test"))
            .build();

        assertThatCode(() -> {
            CreateEmbeddingResponse result = tracedClient.createEmbedding(params);
            assertThat(result).isNotNull();
            assertThat(result.data()).isNotEmpty();
            assertThat(result.data().get(0).embedding()).isNotEmpty();
            System.out.println("[E2E] Embedding dimensions: " +
                result.data().get(0).embedding().size());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(4)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
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

        assertThatCode(() -> {
            Iterable<ChatCompletionChunk> chunks = tracedClient.streamChatCompletion(params);
            assertThat(chunks).isNotNull();
            int chunkCount = 0;
            for (ChatCompletionChunk chunk : chunks) {
                chunkCount++;
            }
            assertThat(chunkCount).isGreaterThan(0);
            System.out.println("[E2E] Streaming chunks received: " + chunkCount);
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(5)
    void shouldCreateTracedClientSuccessfully() {
        // Verify the traced client wrapper can be created and unwrapped
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
