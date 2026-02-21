package ai.traceai.langchain4j;

import static org.assertj.core.api.Assertions.assertThat;

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

import java.util.List;

/**
 * E2E tests for TracedChatLanguageModel (LangChain4j).
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
 *   <li>FI_PROJECT_NAME - Project name (default: java-langchain4j-e2e)</li>
 *   <li>OPENAI_API_KEY - API key for OpenAI (used as LangChain4j backend)</li>
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
                .secretKey(System.getenv("FI_SECRET_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                    ? System.getenv("FI_PROJECT_NAME")
                    : "java-langchain4j-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String openaiApiKey = System.getenv("OPENAI_API_KEY") != null
            ? System.getenv("OPENAI_API_KEY")
            : "dummy-key-for-e2e";

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

        if (!modelAvailable) {
            tracedModel = new TracedChatLanguageModel(new DummyChatLanguageModel(), tracer, "dummy");
            System.out.println("[E2E] Using dummy LangChain4j model");
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportChatGenerationSpan() {
        try {
            Response<AiMessage> response = tracedModel.generate(List.of(
                UserMessage.from("Say 'Hello from Java E2E test' and nothing else.")
            ));
            System.out.println("[E2E] LangChain4j response: " + response.content().text());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportChatWithSystemMessageSpan() {
        try {
            Response<AiMessage> response = tracedModel.generate(List.of(
                SystemMessage.from("You always reply in exactly 3 words."),
                UserMessage.from("What is Java?")
            ));
            System.out.println("[E2E] LangChain4j response with system: " + response.content().text());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportStringGenerateSpan() {
        try {
            String response = tracedModel.generate("What is 2 + 2? Reply with just the number.");
            System.out.println("[E2E] LangChain4j string response: " + response);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
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
