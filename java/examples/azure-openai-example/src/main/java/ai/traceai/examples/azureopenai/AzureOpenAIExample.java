package ai.traceai.examples.azureopenai;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.azure.openai.TracedAzureOpenAIClient;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.ai.openai.models.*;
import com.azure.core.credential.AzureKeyCredential;

import java.util.ArrayList;
import java.util.List;

/**
 * Example demonstrating TraceAI instrumentation with Azure OpenAI Java SDK.
 *
 * <p>This example shows how to:</p>
 * <ul>
 *   <li>Initialize TraceAI with Azure OpenAI</li>
 *   <li>Trace chat completions</li>
 *   <li>Trace embeddings generation</li>
 *   <li>Trace legacy completions (if available)</li>
 * </ul>
 *
 * <p>To run this example:</p>
 * <pre>
 * export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
 * export AZURE_OPENAI_API_KEY=your-api-key
 * export AZURE_OPENAI_DEPLOYMENT=your-deployment-name
 * export AZURE_OPENAI_EMBEDDING_DEPLOYMENT=your-embedding-deployment
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class AzureOpenAIExample {

    public static void main(String[] args) {
        // ============================================================
        // Step 1: Initialize TraceAI
        // TraceAI provides observability for your AI applications,
        // capturing traces, metrics, and logs for analysis.
        // ============================================================
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-azure-openai-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Get Azure OpenAI configuration from environment
        String endpoint = System.getenv("AZURE_OPENAI_ENDPOINT");
        String apiKey = System.getenv("AZURE_OPENAI_API_KEY");
        String deploymentName = System.getenv("AZURE_OPENAI_DEPLOYMENT");
        String embeddingDeployment = System.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT");

        // Validate required environment variables
        if (endpoint == null || apiKey == null || deploymentName == null) {
            System.err.println("Error: Please set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and AZURE_OPENAI_DEPLOYMENT");
            System.exit(1);
        }

        // Use deployment name for embeddings if not specified
        if (embeddingDeployment == null) {
            embeddingDeployment = "text-embedding-ada-002";
        }

        // ============================================================
        // Step 2: Create the Azure OpenAI client
        // The Azure OpenAI SDK provides access to OpenAI models
        // hosted on Azure infrastructure.
        // ============================================================
        OpenAIClient azureClient = new OpenAIClientBuilder()
            .endpoint(endpoint)
            .credential(new AzureKeyCredential(apiKey))
            .buildClient();

        // ============================================================
        // Step 3: Wrap with TracedAzureOpenAIClient
        // This wrapper automatically captures traces for all API calls,
        // including request parameters, response data, and token usage.
        // ============================================================
        TracedAzureOpenAIClient traced = new TracedAzureOpenAIClient(azureClient);

        // ============================================================
        // Example 1: Chat Completion
        // Demonstrates a simple chat interaction with tracing.
        // TraceAI captures: model, messages, temperature, tokens, etc.
        // ============================================================
        System.out.println("\n1. Chat Completion Example");
        System.out.println("---------------------------");

        List<ChatRequestMessage> messages = new ArrayList<>();
        messages.add(new ChatRequestSystemMessage("You are a helpful assistant that provides concise answers."));
        messages.add(new ChatRequestUserMessage("What is the capital of France? Answer in one sentence."));

        ChatCompletionsOptions chatOptions = new ChatCompletionsOptions(messages)
            .setMaxTokens(100)
            .setTemperature(0.7);

        // The traced client automatically captures this call
        ChatCompletions chatResponse = traced.getChatCompletions(deploymentName, chatOptions);

        // Process the response
        String chatOutput = chatResponse.getChoices().get(0).getMessage().getContent();
        System.out.println("Response: " + chatOutput);

        if (chatResponse.getUsage() != null) {
            System.out.println("Tokens - Prompt: " + chatResponse.getUsage().getPromptTokens()
                + ", Completion: " + chatResponse.getUsage().getCompletionTokens()
                + ", Total: " + chatResponse.getUsage().getTotalTokens());
        }

        // ============================================================
        // Example 2: Embeddings
        // Demonstrates generating text embeddings with tracing.
        // TraceAI captures: model, input texts, dimensions, token usage.
        // ============================================================
        System.out.println("\n2. Embeddings Example");
        System.out.println("---------------------");

        List<String> embeddingInputs = new ArrayList<>();
        embeddingInputs.add("The quick brown fox jumps over the lazy dog.");
        embeddingInputs.add("Machine learning is transforming how we build applications.");

        EmbeddingsOptions embeddingsOptions = new EmbeddingsOptions(embeddingInputs);

        // The traced client captures embedding operations
        Embeddings embeddingsResponse = traced.getEmbeddings(embeddingDeployment, embeddingsOptions);

        System.out.println("Embeddings generated: " + embeddingsResponse.getData().size());
        if (!embeddingsResponse.getData().isEmpty()) {
            EmbeddingItem firstEmbedding = embeddingsResponse.getData().get(0);
            System.out.println("Dimensions: " + firstEmbedding.getEmbedding().size());
            System.out.println("First 5 values: " + firstEmbedding.getEmbedding().subList(0,
                Math.min(5, firstEmbedding.getEmbedding().size())));
        }

        if (embeddingsResponse.getUsage() != null) {
            System.out.println("Tokens used: " + embeddingsResponse.getUsage().getTotalTokens());
        }

        // ============================================================
        // Example 3: Multi-turn Conversation
        // Demonstrates a conversation with system prompt and context.
        // TraceAI captures the full message history for debugging.
        // ============================================================
        System.out.println("\n3. Multi-turn Conversation Example");
        System.out.println("-----------------------------------");

        List<ChatRequestMessage> conversationMessages = new ArrayList<>();

        // System message sets the assistant's behavior
        conversationMessages.add(new ChatRequestSystemMessage(
            "You are a knowledgeable history teacher. Provide educational and engaging responses."
        ));

        // First user message
        conversationMessages.add(new ChatRequestUserMessage(
            "Who was the first person to walk on the moon?"
        ));

        ChatCompletionsOptions conversationOptions = new ChatCompletionsOptions(conversationMessages)
            .setMaxTokens(200)
            .setTemperature(0.5);

        ChatCompletions historyResponse = traced.getChatCompletions(deploymentName, conversationOptions);
        String historyOutput = historyResponse.getChoices().get(0).getMessage().getContent();
        System.out.println("Teacher: " + historyOutput);

        // Continue the conversation - add assistant's response and new user question
        conversationMessages.add(new ChatRequestAssistantMessage(historyOutput));
        conversationMessages.add(new ChatRequestUserMessage("What year did this happen?"));

        // Second turn of conversation
        ChatCompletions followUpResponse = traced.getChatCompletions(deploymentName, conversationOptions);
        String followUpOutput = followUpResponse.getChoices().get(0).getMessage().getContent();
        System.out.println("Teacher (follow-up): " + followUpOutput);

        // ============================================================
        // Example 4: Chat with JSON Response Format (if supported)
        // Demonstrates requesting structured JSON output.
        // ============================================================
        System.out.println("\n4. Structured Output Example");
        System.out.println("----------------------------");

        List<ChatRequestMessage> jsonMessages = new ArrayList<>();
        jsonMessages.add(new ChatRequestSystemMessage(
            "You are a helpful assistant that responds in JSON format. " +
            "When asked about a topic, provide a structured response with 'topic', 'summary', and 'key_points' fields."
        ));
        jsonMessages.add(new ChatRequestUserMessage(
            "Tell me about cloud computing in JSON format."
        ));

        ChatCompletionsOptions jsonOptions = new ChatCompletionsOptions(jsonMessages)
            .setMaxTokens(300)
            .setTemperature(0.3);

        ChatCompletions jsonResponse = traced.getChatCompletions(deploymentName, jsonOptions);
        String jsonOutput = jsonResponse.getChoices().get(0).getMessage().getContent();
        System.out.println("JSON Response: " + jsonOutput);

        // ============================================================
        // Cleanup
        // ============================================================
        System.out.println("\n========================================");
        System.out.println("All examples completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");
        System.out.println();
        System.out.println("Traces captured include:");
        System.out.println("  - Azure OpenAI Chat Completion (4 calls)");
        System.out.println("  - Azure OpenAI Embedding (1 call)");
        System.out.println();
        System.out.println("Each trace includes:");
        System.out.println("  - Request parameters (model, temperature, max_tokens)");
        System.out.println("  - Input/output messages");
        System.out.println("  - Token usage statistics");
        System.out.println("  - Response metadata");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }
}
