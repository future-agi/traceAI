package ai.traceai.semantickernel;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;
import com.microsoft.semantickernel.services.chatcompletion.StreamingChatContent;
import com.microsoft.semantickernel.services.chatcompletion.AuthorRole;
import com.microsoft.semantickernel.services.textembedding.Embedding;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.util.Collections;
import java.util.List;

/**
 * E2E tests for Semantic Kernel traced wrappers.
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy delegates) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-semantic-kernel-e2e)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedSemanticKernelE2ETest {

    private static FITracer tracer;
    private static TracedChatCompletionService tracedChatService;
    private static TracedTextEmbeddingGenerationService tracedEmbeddingService;
    private static TracedKernel tracedKernel;

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
                    : "java-semantic-kernel-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        // Create dummy delegates that return simple responses
        ChatCompletionService dummyChatService = new DummyChatCompletionService();
        tracedChatService = new TracedChatCompletionService(dummyChatService, "dummy-model", "dummy", tracer);

        TextEmbeddingGenerationService dummyEmbeddingService = new DummyEmbeddingService();
        tracedEmbeddingService = new TracedTextEmbeddingGenerationService(dummyEmbeddingService, "dummy-embed-model", "dummy", tracer);

        Kernel kernel = Kernel.builder()
            .withAIService(ChatCompletionService.class, tracedChatService)
            .build();
        tracedKernel = new TracedKernel(kernel, tracer);
    }

    @AfterAll
    static void tearDown() {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportChatCompletionSpan() {
        try {
            ChatHistory chatHistory = new ChatHistory();
            chatHistory.addUserMessage("Say 'Hello from Java E2E test' and nothing else.");

            List<ChatMessageContent<?>> result = tracedChatService
                .getChatMessageContentsAsync(chatHistory, null, null)
                .block();

            System.out.println("[E2E] Semantic Kernel chat response: " +
                (result != null && !result.isEmpty() ? result.get(0).getContent() : "(empty)"));
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportEmbeddingGenerationSpan() {
        try {
            List<Embedding> result = tracedEmbeddingService
                .generateEmbeddingsAsync(List.of("Hello from Java E2E test"))
                .block();

            System.out.println("[E2E] Semantic Kernel embedding count: " +
                (result != null ? result.size() : 0));
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportKernelInvokePromptSpan() {
        try {
            tracedKernel.invokePromptAsync("What is 2 + 2?").block();
            System.out.println("[E2E] Semantic Kernel invokePromptAsync succeeded");
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldCreateTracedWrappersSuccessfully() {
        assertThat(tracedChatService).isNotNull();
        assertThat(tracedChatService.unwrap()).isNotNull();
        assertThat(tracedEmbeddingService).isNotNull();
        assertThat(tracedEmbeddingService.unwrap()).isNotNull();
        assertThat(tracedKernel).isNotNull();
        assertThat(tracedKernel.unwrap()).isNotNull();
    }

    // --- Dummy implementations ---

    private static class DummyChatCompletionService implements ChatCompletionService {
        @Override
        public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(
                ChatHistory chatHistory,
                com.microsoft.semantickernel.Kernel kernel,
                com.microsoft.semantickernel.orchestration.InvocationContext invocationContext) {
            ChatMessageContent<?> response = new ChatMessageContent<>(
                AuthorRole.ASSISTANT, "dummy response from E2E test");
            return Mono.just(Collections.singletonList(response));
        }

        @Override
        public Mono<List<ChatMessageContent<?>>> getChatMessageContentsAsync(
                String prompt,
                com.microsoft.semantickernel.Kernel kernel,
                com.microsoft.semantickernel.orchestration.InvocationContext invocationContext) {
            ChatMessageContent<?> response = new ChatMessageContent<>(
                AuthorRole.ASSISTANT, "dummy response from E2E test");
            return Mono.just(Collections.singletonList(response));
        }

        @Override
        public Flux<StreamingChatContent<?>> getStreamingChatMessageContentsAsync(
                ChatHistory chatHistory,
                com.microsoft.semantickernel.Kernel kernel,
                com.microsoft.semantickernel.orchestration.InvocationContext invocationContext) {
            return Flux.empty();
        }

        @Override
        public Flux<StreamingChatContent<?>> getStreamingChatMessageContentsAsync(
                String prompt,
                com.microsoft.semantickernel.Kernel kernel,
                com.microsoft.semantickernel.orchestration.InvocationContext invocationContext) {
            return Flux.empty();
        }

        @Override
        public String getModelId() { return "dummy-model"; }

        @Override
        public String getServiceId() { return "dummy-service"; }
    }

    private static class DummyEmbeddingService implements TextEmbeddingGenerationService {
        @Override
        public Mono<List<Embedding>> generateEmbeddingsAsync(List<String> data) {
            List<Embedding> embeddings = Collections.singletonList(
                new Embedding(List.of(0.1f, 0.2f, 0.3f)));
            return Mono.just(embeddings);
        }

        @Override
        public Mono<Embedding> generateEmbeddingAsync(String text) {
            return Mono.just(new Embedding(List.of(0.1f, 0.2f, 0.3f)));
        }

        @Override
        public String getModelId() { return "dummy-embed-model"; }

        @Override
        public String getServiceId() { return "dummy-embed-service"; }
    }
}
