package ai.traceai.examples.semantickernel;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.semantickernel.TracedKernel;
import ai.traceai.semantickernel.TracedChatCompletionService;
import com.azure.ai.openai.OpenAIAsyncClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.credential.KeyCredential;
import com.microsoft.semantickernel.Kernel;
import com.microsoft.semantickernel.aiservices.openai.chatcompletion.OpenAIChatCompletion;
import com.microsoft.semantickernel.orchestration.FunctionResult;
import com.microsoft.semantickernel.orchestration.PromptExecutionSettings;
import com.microsoft.semantickernel.semanticfunctions.KernelFunction;
import com.microsoft.semantickernel.semanticfunctions.KernelFunctionArguments;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent;

import java.util.List;

/**
 * Example demonstrating TraceAI instrumentation with Microsoft Semantic Kernel.
 *
 * <p>This example shows how to:</p>
 * <ul>
 *   <li>Initialize TraceAI with Semantic Kernel</li>
 *   <li>Use TracedKernel for function invocations</li>
 *   <li>Use TracedChatCompletionService for chat operations</li>
 *   <li>Create and trace custom prompts</li>
 * </ul>
 *
 * <p>To run this example:</p>
 * <pre>
 * # For OpenAI:
 * export OPENAI_API_KEY=your-api-key
 *
 * # For Azure OpenAI:
 * export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
 * export AZURE_OPENAI_API_KEY=your-api-key
 * export AZURE_OPENAI_DEPLOYMENT=your-deployment-name
 *
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class SemanticKernelExample {

    public static void main(String[] args) {
        // ============================================================
        // Step 1: Initialize TraceAI
        // TraceAI provides observability for your AI applications,
        // capturing traces for Semantic Kernel operations.
        // ============================================================
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-semantic-kernel-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Determine which provider to use
        String openaiKey = System.getenv("OPENAI_API_KEY");
        String azureEndpoint = System.getenv("AZURE_OPENAI_ENDPOINT");
        String azureKey = System.getenv("AZURE_OPENAI_API_KEY");
        String azureDeployment = System.getenv("AZURE_OPENAI_DEPLOYMENT");

        boolean useAzure = azureEndpoint != null && azureKey != null;

        if (openaiKey == null && !useAzure) {
            System.err.println("Error: Please set either OPENAI_API_KEY or Azure OpenAI credentials");
            System.err.println();
            printExampleUsage();
            TraceAI.shutdown();
            System.exit(1);
        }

        String modelName = useAzure ? (azureDeployment != null ? azureDeployment : "gpt-4") : "gpt-4o-mini";
        String provider = useAzure ? "azure" : "openai";

        System.out.println("Provider: " + provider);
        System.out.println("Model: " + modelName);

        try {
            // ============================================================
            // Step 2: Create the OpenAI client
            // ============================================================
            OpenAIAsyncClient openAIClient;

            if (useAzure) {
                openAIClient = new OpenAIClientBuilder()
                    .endpoint(azureEndpoint)
                    .credential(new AzureKeyCredential(azureKey))
                    .buildAsyncClient();
            } else {
                openAIClient = new OpenAIClientBuilder()
                    .credential(new KeyCredential(openaiKey))
                    .buildAsyncClient();
            }

            // ============================================================
            // Step 3: Create ChatCompletionService and wrap with tracing
            // TracedChatCompletionService captures all chat interactions.
            // ============================================================
            ChatCompletionService baseChatService = OpenAIChatCompletion.builder()
                .withModelId(modelName)
                .withOpenAIAsyncClient(openAIClient)
                .build();

            TracedChatCompletionService tracedChatService = new TracedChatCompletionService(
                baseChatService,
                modelName,
                provider
            );

            // ============================================================
            // Step 4: Build the Kernel with traced service
            // The Kernel is the central component of Semantic Kernel.
            // ============================================================
            Kernel kernel = Kernel.builder()
                .withAIService(ChatCompletionService.class, tracedChatService)
                .build();

            // Wrap with TracedKernel for function invocation tracing
            TracedKernel tracedKernel = new TracedKernel(kernel);

            // ============================================================
            // Example 1: TracedChatCompletionService - Simple Chat
            // Demonstrates direct chat completion with tracing.
            // TraceAI captures: model, messages, response, tokens.
            // ============================================================
            System.out.println("\n1. Chat Completion Example");
            System.out.println("---------------------------");

            ChatHistory chatHistory = new ChatHistory();
            chatHistory.addSystemMessage("You are a helpful assistant that provides concise answers.");
            chatHistory.addUserMessage("What is the capital of France?");

            List<ChatMessageContent<?>> chatResponse = tracedChatService
                .getChatMessageContentsAsync(chatHistory, null, null)
                .block();

            if (chatResponse != null && !chatResponse.isEmpty()) {
                System.out.println("Response: " + chatResponse.get(0).getContent());
            }

            // ============================================================
            // Example 2: TracedKernel - Prompt Invocation
            // Demonstrates invoking a prompt through the kernel.
            // TraceAI captures: function name, arguments, result.
            // ============================================================
            System.out.println("\n2. Kernel Prompt Invocation Example");
            System.out.println("------------------------------------");

            FunctionResult<String> promptResult = tracedKernel.invokePromptAsync(
                "Summarize the concept of {{$topic}} in one sentence.",
                KernelFunctionArguments.builder()
                    .withVariable("topic", "machine learning")
                    .build()
            ).block();

            if (promptResult != null) {
                System.out.println("Summary: " + promptResult.getResult());
            }

            // ============================================================
            // Example 3: Multi-turn Conversation
            // Demonstrates context preservation across turns.
            // TraceAI captures: full message history per call.
            // ============================================================
            System.out.println("\n3. Multi-turn Conversation Example");
            System.out.println("-----------------------------------");

            ChatHistory conversation = new ChatHistory();
            conversation.addSystemMessage("You are a knowledgeable history teacher.");

            // First turn
            conversation.addUserMessage("Who was the first president of the United States?");
            List<ChatMessageContent<?>> turn1 = tracedChatService
                .getChatMessageContentsAsync(conversation, null, null)
                .block();

            if (turn1 != null && !turn1.isEmpty()) {
                String response1 = turn1.get(0).getContent();
                System.out.println("Q: Who was the first president?");
                System.out.println("A: " + response1);
                conversation.addAssistantMessage(response1);
            }

            // Second turn (context maintained)
            conversation.addUserMessage("What year was he inaugurated?");
            List<ChatMessageContent<?>> turn2 = tracedChatService
                .getChatMessageContentsAsync(conversation, null, null)
                .block();

            if (turn2 != null && !turn2.isEmpty()) {
                System.out.println("Q: What year was he inaugurated?");
                System.out.println("A: " + turn2.get(0).getContent());
            }

            // ============================================================
            // Example 4: Custom Kernel Function
            // Demonstrates creating and invoking a kernel function.
            // TraceAI captures: function name, plugin name, execution.
            // ============================================================
            System.out.println("\n4. Custom Kernel Function Example");
            System.out.println("----------------------------------");

            // Create a simple prompt function
            KernelFunction<String> translateFunction = KernelFunction.<String>createFromPrompt(
                """
                Translate the following text to {{$language}}:

                {{$text}}

                Translation:
                """
            ).build();

            FunctionResult<String> translateResult = tracedKernel.invokeAsync(
                translateFunction,
                KernelFunctionArguments.builder()
                    .withVariable("language", "French")
                    .withVariable("text", "Hello, how are you today?")
                    .build()
            ).block();

            if (translateResult != null) {
                System.out.println("Translation: " + translateResult.getResult());
            }

            // ============================================================
            // Example 5: Code Generation
            // Demonstrates using Semantic Kernel for code generation.
            // ============================================================
            System.out.println("\n5. Code Generation Example");
            System.out.println("---------------------------");

            FunctionResult<String> codeResult = tracedKernel.invokePromptAsync(
                """
                Write a simple {{$language}} function that {{$task}}.
                Only provide the code, no explanations.
                """,
                KernelFunctionArguments.builder()
                    .withVariable("language", "Python")
                    .withVariable("task", "calculates the factorial of a number")
                    .build()
            ).block();

            if (codeResult != null) {
                System.out.println("Generated code:");
                System.out.println(codeResult.getResult());
            }

            // ============================================================
            // Example 6: Q&A with Context
            // Demonstrates retrieval-augmented style prompting.
            // ============================================================
            System.out.println("\n6. Q&A with Context Example");
            System.out.println("----------------------------");

            String context = """
                TraceAI is an enterprise-grade AI observability platform.
                It provides tracing, monitoring, and debugging capabilities for AI applications.
                TraceAI supports multiple LLM providers and vector databases.
                The platform uses OpenTelemetry for standardized telemetry collection.
                """;

            FunctionResult<String> qaResult = tracedKernel.invokePromptAsync(
                """
                Based on the following context, answer the question.

                Context:
                {{$context}}

                Question: {{$question}}

                Answer:
                """,
                KernelFunctionArguments.builder()
                    .withVariable("context", context)
                    .withVariable("question", "What telemetry standard does TraceAI use?")
                    .build()
            ).block();

            if (qaResult != null) {
                System.out.println("Answer: " + qaResult.getResult());
            }

        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
        }

        // ============================================================
        // Summary
        // ============================================================
        System.out.println("\n========================================");
        System.out.println("All examples completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");
        System.out.println();
        System.out.println("Traces captured include:");
        System.out.println("  - Semantic Kernel Chat Completion (4 calls)");
        System.out.println("  - Semantic Kernel Prompt (4 function invocations)");
        System.out.println();
        System.out.println("Each trace includes:");
        System.out.println("  - Model and provider information");
        System.out.println("  - Function name and plugin (if applicable)");
        System.out.println("  - Input arguments and output result");
        System.out.println("  - Message history (for chat completions)");
        System.out.println("  - Token usage (when available)");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }

    /**
     * Prints example usage.
     */
    private static void printExampleUsage() {
        System.out.println("Example setup:");
        System.out.println();
        System.out.println("For OpenAI:");
        System.out.println("  export OPENAI_API_KEY=sk-...");
        System.out.println();
        System.out.println("For Azure OpenAI:");
        System.out.println("  export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/");
        System.out.println("  export AZURE_OPENAI_API_KEY=your-key");
        System.out.println("  export AZURE_OPENAI_DEPLOYMENT=gpt-4");
        System.out.println();
        System.out.println("Example code:");
        System.out.println("""

            // Create chat service
            ChatCompletionService chatService = OpenAIChatCompletion.builder()
                .withModelId("gpt-4")
                .withOpenAIAsyncClient(client)
                .build();

            // Wrap with tracing
            TracedChatCompletionService tracedService = new TracedChatCompletionService(
                chatService, "gpt-4", "openai"
            );

            // Build kernel
            Kernel kernel = Kernel.builder()
                .withAIService(ChatCompletionService.class, tracedService)
                .build();

            // Wrap kernel for function tracing
            TracedKernel tracedKernel = new TracedKernel(kernel);

            // Use traced service directly for chat
            ChatHistory history = new ChatHistory();
            history.addUserMessage("Hello!");
            List<ChatMessageContent<?>> response = tracedService
                .getChatMessageContentsAsync(history, null, null)
                .block();

            // Use traced kernel for prompts
            FunctionResult<String> result = tracedKernel.invokePromptAsync(
                "Summarize: {{$text}}",
                KernelFunctionArguments.builder()
                    .withVariable("text", "...")
                    .build()
            ).block();
            """);
    }
}
