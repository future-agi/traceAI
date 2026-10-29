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
 * export FI_BASE_URL=https://api.futureagi.com
 * export FI_API_KEY=your-futureagi-api-key
 * export FI_SECRET_KEY=your-futureagi-secret-key
 * export FI_PROJECT_NAME=java-openai-example
 * mvn compile exec:java -Dexec.args=--smoke
 * // OPENAI_BASE_URL and OPENAI_MODEL optionally select an OpenAI-compatible endpoint.
 * </pre>
 */
public class OpenAIExample {

    public static void main(String[] args) {
        // Initialize TraceAI
        TraceAI.init(TraceConfig.builder()
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        try {

            System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
            System.out.println("========================================");

            // Create OpenAI client
            OpenAIClient openai = OpenAIOkHttpClient.builder()
                .apiKey(System.getenv("OPENAI_API_KEY"))
                .baseUrl(System.getenv().getOrDefault("OPENAI_BASE_URL", "https://api.openai.com/v1"))
                .build();
            String model = System.getenv().getOrDefault("OPENAI_MODEL", "gpt-4o-mini");

            // Wrap with tracing
            TracedOpenAIClient traced = new TracedOpenAIClient(openai);

            // Example 1: Chat completion
            System.out.println("\n1. Chat Completion Example");
            System.out.println("---------------------------");

            ChatCompletion chatResponse = traced.createChatCompletion(
                ChatCompletionCreateParams.builder()
                    .model(model)
                    .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                        ChatCompletionUserMessageParam.builder()
                            .role(ChatCompletionUserMessageParam.Role.USER)
                            .content(ChatCompletionUserMessageParam.Content.ofTextContent(
                                "What is the capital of France? Answer in one sentence."
                            ))
                            .build()
                    ))
                    .maxTokens(100)
                    .build()
            );

            String chatOutput = chatResponse.choices().get(0).message().content().orElse("No response");
            System.out.println("Response: " + chatOutput);

            chatResponse.usage().ifPresent(usage ->
                System.out.println("Tokens - Prompt: " + usage.promptTokens()
                    + ", Completion: " + usage.completionTokens()
                    + ", Total: " + usage.totalTokens()));

            // One small chat request is enough to verify an endpoint and Cloud export.
            if (args.length > 0 && "--smoke".equals(args[0])) {
                return;
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
                    .model(model)
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

        } finally {
            // Shutdown to flush spans even if a provider request fails.
            TraceAI.shutdown();
        }
    }
}
