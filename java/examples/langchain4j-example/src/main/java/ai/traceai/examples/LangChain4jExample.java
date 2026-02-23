package ai.traceai.examples;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.langchain4j.TracedChatLanguageModel;
import ai.traceai.langchain4j.TracedAiServices;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.service.AiServices;

import java.util.List;

/**
 * Example demonstrating TraceAI instrumentation with LangChain4j.
 *
 * <p>To run this example:</p>
 * <pre>
 * export OPENAI_API_KEY=your-api-key
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class LangChain4jExample {

    // Define an AiService interface
    interface Assistant {
        String chat(String message);
        String summarize(String text);
    }

    public static void main(String[] args) {
        // Initialize TraceAI
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-langchain4j-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Create the base model
        ChatLanguageModel baseModel = OpenAiChatModel.builder()
            .apiKey(System.getenv("OPENAI_API_KEY"))
            .modelName("gpt-4o-mini")
            .maxTokens(200)
            .build();

        // Example 1: Traced ChatLanguageModel
        System.out.println("\n1. Traced ChatLanguageModel Example");
        System.out.println("------------------------------------");

        ChatLanguageModel tracedModel = new TracedChatLanguageModel(baseModel, "openai");

        String response = tracedModel.generate("What are the three laws of robotics?");
        System.out.println("Response: " + response);

        // Example 2: Traced AiService
        System.out.println("\n2. Traced AiService Example");
        System.out.println("----------------------------");

        // Build the service
        Assistant baseAssistant = AiServices.builder(Assistant.class)
            .chatLanguageModel(baseModel)
            .build();

        // Wrap with tracing
        Assistant tracedAssistant = TracedAiServices.create(Assistant.class, baseAssistant);

        // Use the traced service
        String chatResponse = tracedAssistant.chat("Hello! How are you?");
        System.out.println("Chat Response: " + chatResponse);

        String summaryResponse = tracedAssistant.summarize(
            "Artificial intelligence (AI) is the simulation of human intelligence processes " +
            "by machines, especially computer systems. Specific applications of AI include " +
            "expert systems, natural language processing, speech recognition and machine vision."
        );
        System.out.println("\nSummary Response: " + summaryResponse);

        // Example 3: Complex conversation with TracedChatLanguageModel
        System.out.println("\n3. Multi-turn Conversation Example");
        System.out.println("-----------------------------------");

        var messages = List.of(
            dev.langchain4j.data.message.SystemMessage.from("You are a helpful coding assistant."),
            dev.langchain4j.data.message.UserMessage.from("How do I sort a list in Java?")
        );

        var multiTurnResponse = tracedModel.generate(messages);
        System.out.println("Coding Response: " + multiTurnResponse.content().text());

        if (multiTurnResponse.tokenUsage() != null) {
            System.out.println("Tokens - Input: " + multiTurnResponse.tokenUsage().inputTokenCount()
                + ", Output: " + multiTurnResponse.tokenUsage().outputTokenCount());
        }

        System.out.println("\n========================================");
        System.out.println("All examples completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }
}
