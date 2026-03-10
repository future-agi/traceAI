package ai.traceai.examples;

import ai.traceai.FITracer;
import ai.traceai.spring.TracedChatModel;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.web.bind.annotation.*;

/**
 * Example Spring Boot application demonstrating TraceAI with Spring AI.
 *
 * <p>To run this example:</p>
 * <pre>
 * export SPRING_AI_OPENAI_API_KEY=your-api-key
 * mvn spring-boot:run
 * </pre>
 *
 * <p>Configuration in application.yml:</p>
 * <pre>
 * traceai:
 *   api-key: ${TRACEAI_API_KEY}
 *   project-name: spring-ai-example
 *   enable-console-exporter: true
 *
 * spring:
 *   ai:
 *     openai:
 *       api-key: ${SPRING_AI_OPENAI_API_KEY}
 * </pre>
 */
@SpringBootApplication
public class SpringAIExampleApplication {

    public static void main(String[] args) {
        SpringApplication.run(SpringAIExampleApplication.class, args);
    }

    /**
     * Create a traced chat model that wraps the auto-configured OpenAI model.
     */
    @Bean
    public TracedChatModel tracedChatModel(ChatModel chatModel, FITracer tracer) {
        return new TracedChatModel(chatModel, tracer, "openai");
    }

    /**
     * Demo runner that shows tracing in action.
     */
    @Bean
    public CommandLineRunner demo(TracedChatModel tracedChatModel) {
        return args -> {
            System.out.println("\n========================================");
            System.out.println("Spring AI with TraceAI Example");
            System.out.println("========================================\n");

            // Simple chat
            var response = tracedChatModel.call(
                new Prompt("What is 2 + 2? Answer in one word.")
            );

            System.out.println("Response: " + response.getResult().getOutput().getContent());

            if (response.getMetadata() != null && response.getMetadata().getUsage() != null) {
                var usage = response.getMetadata().getUsage();
                System.out.println("Tokens - Prompt: " + usage.getPromptTokens()
                    + ", Generation: " + usage.getGenerationTokens());
            }

            System.out.println("\nTraces are being exported to TraceAI.");
            System.out.println("Try the REST endpoint: curl http://localhost:8080/chat?message=Hello");
        };
    }
}

/**
 * REST controller for chat interactions.
 */
@RestController
@RequestMapping("/chat")
class ChatController {

    private final TracedChatModel chatModel;

    @Autowired
    public ChatController(TracedChatModel chatModel) {
        this.chatModel = chatModel;
    }

    @GetMapping
    public String chat(@RequestParam String message) {
        var response = chatModel.call(new Prompt(message));
        return response.getResult().getOutput().getContent();
    }

    @PostMapping
    public String chatPost(@RequestBody ChatRequest request) {
        var response = chatModel.call(new Prompt(request.message()));
        return response.getResult().getOutput().getContent();
    }

    record ChatRequest(String message) {}
}
