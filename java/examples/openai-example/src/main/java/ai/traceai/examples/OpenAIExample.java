package ai.traceai.examples;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.openai.TracedOpenAIClient;
import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;
import com.openai.models.*;

/**
 * Example demonstrating TraceAI instrumentation with OpenAI Java SDK.
 *
 * <p>To run this example:</p>
 * <pre>
 * export OPENAI_API_KEY=your-api-key
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class OpenAIExample {

    public static void main(String[] args) {
        // Initialize TraceAI
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-openai-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Create OpenAI client
        OpenAIClient openai = OpenAIOkHttpClient.builder()
            .apiKey(System.getenv("OPENAI_API_KEY"))
            .build();

        // Wrap with tracing
        TracedOpenAIClient traced = new TracedOpenAIClient(openai);

        // Example 1: Chat completion
        System.out.println("\n1. Chat Completion Example");
        System.out.println("---------------------------");

        ChatCompletion chatResponse = traced.createChatCompletion(
            ChatCompletionCreateParams.builder()
                .model("gpt-4o-mini")
                .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                    ChatCompletionUserMessageParam.builder()
                        .role(ChatCompletionUserMessageParam.Role.USER)
                        .content(ChatCompletionUserMessageParam.Content.ofTextContent(
                            "What is the capital of France? Answer in one sentence."
                        ))
                        .build()
                ))
                .maxTokens(100)
                .temperature(0.7)
                .build()
        );

        String chatOutput = chatResponse.choices().get(0).message().content().orElse("No response");
        System.out.println("Response: " + chatOutput);

        if (chatResponse.usage() != null) {
            System.out.println("Tokens - Prompt: " + chatResponse.usage().promptTokens()
                + ", Completion: " + chatResponse.usage().completionTokens()
                + ", Total: " + chatResponse.usage().totalTokens());
        }

        // Example 2: Embedding
        System.out.println("\n2. Embedding Example");
        System.out.println("--------------------");

        CreateEmbeddingResponse embeddingResponse = traced.createEmbedding(
            EmbeddingCreateParams.builder()
                .model("text-embedding-3-small")
                .input(EmbeddingCreateParams.Input.ofString("Hello, world!"))
                .build()
        );

        System.out.println("Embeddings generated: " + embeddingResponse.data().size());
        System.out.println("Dimensions: " + embeddingResponse.data().get(0).embedding().size());
        System.out.println("First 5 values: " +
            embeddingResponse.data().get(0).embedding().subList(0, 5));

        // Example 3: Multi-turn conversation
        System.out.println("\n3. Multi-turn Conversation Example");
        System.out.println("-----------------------------------");

        ChatCompletion conversationResponse = traced.createChatCompletion(
            ChatCompletionCreateParams.builder()
                .model("gpt-4o-mini")
                .addMessage(ChatCompletionMessageParam.ofChatCompletionSystemMessageParam(
                    ChatCompletionSystemMessageParam.builder()
                        .role(ChatCompletionSystemMessageParam.Role.SYSTEM)
                        .content(ChatCompletionSystemMessageParam.Content.ofTextContent(
                            "You are a helpful assistant that speaks like a pirate."
                        ))
                        .build()
                ))
                .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                    ChatCompletionUserMessageParam.builder()
                        .role(ChatCompletionUserMessageParam.Role.USER)
                        .content(ChatCompletionUserMessageParam.Content.ofTextContent(
                            "What is the weather like today?"
                        ))
                        .build()
                ))
                .maxTokens(150)
                .build()
        );

        String pirateOutput = conversationResponse.choices().get(0).message().content().orElse("No response");
        System.out.println("Pirate Response: " + pirateOutput);

        System.out.println("\n========================================");
        System.out.println("All examples completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }
}
