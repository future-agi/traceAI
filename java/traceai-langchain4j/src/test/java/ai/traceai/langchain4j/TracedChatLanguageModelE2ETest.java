package ai.traceai.langchain4j;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.lang.reflect.Method;
import java.util.List;

/**
 * E2E tests for TracedChatLanguageModel (LangChain4j).
 *
 * <p>These tests make REAL API calls through a LangChain4j model and verify
 * that spans are exported to the FI backend for visual verification. They
 * are separate from the mock-based unit tests in TracedChatLanguageModelTest.</p>
 *
 * <p>The test uses an OpenAI-backed LangChain4j model by default. Other
 * providers can be used by modifying the client construction.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>OPENAI_API_KEY - API key for OpenAI (used as LangChain4j backend)</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-langchain4j-e2e)</li>
 *   <li>OPENAI_BASE_URL - OpenAI base URL (for compatible endpoints)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedChatLanguageModelE2ETest {

    private static FITracer tracer;
    private static TracedChatLanguageModel tracedModel;
    private static boolean modelAvailable = false;

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
                    : "java-langchain4j-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        // Try to create an OpenAI-backed LangChain4j model via reflection
        // This avoids a hard compile-time dependency on langchain4j-openai
        String openaiApiKey = System.getenv("OPENAI_API_KEY");
        if (openaiApiKey != null) {
            try {
                ChatLanguageModel delegate = createOpenAiChatModel(openaiApiKey);
                if (delegate != null) {
                    tracedModel = new TracedChatLanguageModel(delegate, tracer, "openai");
                    modelAvailable = true;
                    System.out.println("[E2E] LangChain4j OpenAI model created successfully");
                }
            } catch (Exception e) {
                System.out.println("[E2E] Could not create LangChain4j OpenAI model: " + e.getMessage());
            }
        }

        // Fallback: create a dummy traced model for wrapper tests
        if (!modelAvailable) {
            tracedModel = new TracedChatLanguageModel(new DummyChatLanguageModel(), tracer, "dummy");
            System.out.println("[E2E] Using dummy LangChain4j model");
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000); // Allow batch export to flush
    }

    @Test
    @Order(1)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
    void shouldExportChatGenerationSpan() {
        Assumptions.assumeTrue(modelAvailable, "LangChain4j OpenAI model not available");

        assertThatCode(() -> {
            Response<AiMessage> response = tracedModel.generate(List.of(
                UserMessage.from("Say 'Hello from Java E2E test' and nothing else.")
            ));
            assertThat(response).isNotNull();
            assertThat(response.content()).isNotNull();
            assertThat(response.content().text()).isNotEmpty();
            System.out.println("[E2E] LangChain4j response: " + response.content().text());

            if (response.tokenUsage() != null) {
                System.out.println("[E2E] LangChain4j tokens - input: " +
                    response.tokenUsage().inputTokenCount() +
                    ", output: " + response.tokenUsage().outputTokenCount());
            }
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(2)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
    void shouldExportChatWithSystemMessageSpan() {
        Assumptions.assumeTrue(modelAvailable, "LangChain4j OpenAI model not available");

        assertThatCode(() -> {
            Response<AiMessage> response = tracedModel.generate(List.of(
                SystemMessage.from("You always reply in exactly 3 words."),
                UserMessage.from("What is Java?")
            ));
            assertThat(response).isNotNull();
            assertThat(response.content()).isNotNull();
            System.out.println("[E2E] LangChain4j response with system: " + response.content().text());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(3)
    @EnabledIfEnvironmentVariable(named = "OPENAI_API_KEY", matches = ".+")
    void shouldExportStringGenerateSpan() {
        Assumptions.assumeTrue(modelAvailable, "LangChain4j OpenAI model not available");

        assertThatCode(() -> {
            String response = tracedModel.generate("What is 2 + 2? Reply with just the number.");
            assertThat(response).isNotNull();
            assertThat(response).isNotEmpty();
            System.out.println("[E2E] LangChain4j string response: " + response);
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(4)
    void shouldCreateTracedModelSuccessfully() {
        assertThat(tracedModel).isNotNull();
        assertThat(tracedModel.unwrap()).isNotNull();
    }

    // --- Helper: reflectively create OpenAiChatModel ---

    private static ChatLanguageModel createOpenAiChatModel(String apiKey) {
        try {
            Class<?> modelClass = Class.forName("dev.langchain4j.model.openai.OpenAiChatModel");
            Object builder = modelClass.getMethod("builder").invoke(null);

            builder.getClass().getMethod("apiKey", String.class).invoke(builder, apiKey);
            builder.getClass().getMethod("modelName", String.class).invoke(builder, "gpt-4o-mini");
            builder.getClass().getMethod("maxTokens", Integer.class).invoke(builder, 100);

            String baseUrl = System.getenv("OPENAI_BASE_URL");
            if (baseUrl != null) {
                builder.getClass().getMethod("baseUrl", String.class).invoke(builder, baseUrl);
            }

            return (ChatLanguageModel) builder.getClass().getMethod("build").invoke(builder);
        } catch (ClassNotFoundException e) {
            System.out.println("[E2E] langchain4j-openai not on classpath: " + e.getMessage());
            return null;
        } catch (Exception e) {
            System.out.println("[E2E] Error creating OpenAiChatModel: " + e.getMessage());
            return null;
        }
    }

    // --- Dummy model for wrapper creation tests ---

    private static class DummyChatLanguageModel implements ChatLanguageModel {
        @Override
        public Response<AiMessage> generate(List<ChatMessage> messages) {
            return Response.from(AiMessage.from("dummy response"));
        }

        @Override
        public String generate(String text) {
            return "dummy response";
        }
    }
}
