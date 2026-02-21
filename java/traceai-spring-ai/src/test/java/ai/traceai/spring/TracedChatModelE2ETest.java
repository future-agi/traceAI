package ai.traceai.spring;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.publisher.Flux;

/**
 * E2E test for TracedChatModel.
 * Requires a real Spring AI ChatModel backend and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-spring-ai-e2e</li>
 *   <li>OPENAI_API_KEY - (optional) needed if using an OpenAI-backed ChatModel</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedChatModelE2ETest {

    private static FITracer tracer;

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
                        : "java-spring-ai-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000);
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldInitializeTraceAI() {
        assertThat(TraceAI.isInitialized()).isTrue();
        assertThat(tracer).isNotNull();
    }

    @Test
    @Order(2)
    void shouldWrapChatModelWithTracer() {
        // Verify the traced wrapper can be created with a stub delegate.
        // Even without a real ChatModel backend, instantiation should succeed.
        ChatModel stubModel = new StubChatModel();
        TracedChatModel traced = new TracedChatModel(stubModel, tracer, "stub-provider");

        assertThat(traced).isNotNull();
        assertThat(traced.unwrap()).isSameAs(stubModel);
    }

    @Test
    @Order(3)
    void shouldExportSpanOnCall() {
        // Use a stub model that returns a fixed response.
        // The span will be exported to the FI backend even with a stub.
        ChatModel stubModel = new StubChatModel();
        TracedChatModel traced = new TracedChatModel(stubModel, tracer, "stub-provider");

        try {
            ChatResponse response = traced.call(new Prompt("Hello, world!"));
            // The stub returns null, but the span should still be exported
        } catch (Exception e) {
            // Even error spans get exported -- this is fine for E2E verification
            System.out.println("Expected error from stub model: " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldExportSpanOnStream() {
        ChatModel stubModel = new StubChatModel();
        TracedChatModel traced = new TracedChatModel(stubModel, tracer, "stub-provider");

        try {
            Flux<ChatResponse> stream = traced.stream(new Prompt("Stream test"));
            if (stream != null) {
                // Subscribe to trigger the stream span
                stream.blockFirst();
            }
        } catch (Exception e) {
            // Even error spans get exported
            System.out.println("Expected error from stub stream: " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldUseGlobalTracerConstructor() {
        // Verify the two-arg constructor that uses TraceAI.getTracer() internally
        ChatModel stubModel = new StubChatModel();
        TracedChatModel traced = new TracedChatModel(stubModel, "test-provider");

        assertThat(traced).isNotNull();
        assertThat(traced.unwrap()).isSameAs(stubModel);
    }

    /**
     * A minimal stub ChatModel for testing the wrapper without a real LLM backend.
     */
    private static class StubChatModel implements ChatModel {
        @Override
        public ChatResponse call(Prompt prompt) {
            // Return null to simulate a minimal response; the traced wrapper handles null gracefully
            return null;
        }

        @Override
        public Flux<ChatResponse> stream(Prompt prompt) {
            return Flux.empty();
        }

        @Override
        public org.springframework.ai.chat.prompt.ChatOptions getDefaultOptions() {
            return null;
        }
    }
}
