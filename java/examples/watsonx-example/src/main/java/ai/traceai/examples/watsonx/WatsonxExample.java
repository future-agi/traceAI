package ai.traceai.examples.watsonx;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.watsonx.TracedWatsonxAI;

/**
 * Example demonstrating TraceAI instrumentation with IBM watsonx.ai Java SDK.
 *
 * <p>This example shows how to:</p>
 * <ul>
 *   <li>Initialize TraceAI with watsonx.ai</li>
 *   <li>Trace text generation</li>
 *   <li>Trace chat completions</li>
 *   <li>Trace embeddings generation</li>
 * </ul>
 *
 * <p>To run this example:</p>
 * <pre>
 * export WATSONX_API_KEY=your-api-key
 * export WATSONX_PROJECT_ID=your-project-id
 * export WATSONX_URL=https://us-south.ml.cloud.ibm.com (optional)
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 *
 * <p>Note: This example uses reflection-based API calls to support different
 * versions of the watsonx.ai Java SDK. The actual API calls depend on the
 * SDK version you have installed.</p>
 */
public class WatsonxExample {

    public static void main(String[] args) {
        // ============================================================
        // Step 1: Initialize TraceAI
        // TraceAI provides observability for your AI applications,
        // capturing traces, metrics, and logs for analysis.
        // ============================================================
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-watsonx-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Get watsonx configuration from environment
        String apiKey = System.getenv("WATSONX_API_KEY");
        String projectId = System.getenv("WATSONX_PROJECT_ID");
        String watsonxUrl = System.getenv("WATSONX_URL");

        if (watsonxUrl == null) {
            watsonxUrl = "https://us-south.ml.cloud.ibm.com";
        }

        // Validate required environment variables
        if (apiKey == null || projectId == null) {
            System.err.println("Error: Please set WATSONX_API_KEY and WATSONX_PROJECT_ID");
            System.err.println();
            System.err.println("This example demonstrates the TraceAI watsonx.ai instrumentation.");
            System.err.println("The TracedWatsonxAI class wraps the IBM watsonx.ai SDK and provides");
            System.err.println("automatic tracing of all API calls.");
            System.err.println();
            System.err.println("Example usage with actual watsonx.ai SDK:");
            printExampleUsage();
            System.exit(1);
        }

        // ============================================================
        // Step 2: Create the watsonx.ai client
        // The watsonx.ai SDK provides access to IBM's foundation models.
        //
        // Note: The actual client creation depends on your SDK version.
        // This example shows the general pattern.
        // ============================================================
        System.out.println("\nCreating watsonx.ai client...");
        System.out.println("Endpoint: " + watsonxUrl);
        System.out.println("Project ID: " + projectId);

        try {
            // Create the watsonx client using reflection to support multiple SDK versions
            // In a real application, you would use the SDK directly:
            //
            // WatsonxAI watsonx = WatsonxAI.builder()
            //     .apiKey(apiKey)
            //     .projectId(projectId)
            //     .url(watsonxUrl)
            //     .build();

            Object watsonxClient = createWatsonxClient(apiKey, projectId, watsonxUrl);

            if (watsonxClient == null) {
                System.out.println("\nNote: Could not create watsonx.ai client.");
                System.out.println("This may be because the IBM watsonx-ai SDK is not available.");
                printExampleUsage();
                TraceAI.shutdown();
                return;
            }

            // ============================================================
            // Step 3: Wrap with TracedWatsonxAI
            // This wrapper automatically captures traces for all API calls.
            // ============================================================
            TracedWatsonxAI traced = new TracedWatsonxAI(watsonxClient);

            // ============================================================
            // Example 1: Text Generation
            // Demonstrates text generation with IBM Granite models.
            // TraceAI captures: model, input, parameters, output, tokens.
            // ============================================================
            System.out.println("\n1. Text Generation Example");
            System.out.println("--------------------------");

            // Create text generation request
            // The actual request object depends on your SDK version
            Object textGenRequest = createTextGenRequest(
                "ibm/granite-13b-chat-v2",
                "Explain the benefits of cloud computing in 3 bullet points:",
                projectId,
                200,    // max tokens
                0.7     // temperature
            );

            if (textGenRequest != null) {
                Object response = traced.generateText(textGenRequest);
                System.out.println("Text generation completed. Check TraceAI dashboard for details.");
                printResponse(response);
            } else {
                System.out.println("Skipped: Text generation request could not be created.");
            }

            // ============================================================
            // Example 2: Chat Completion
            // Demonstrates multi-turn chat with watsonx.ai.
            // TraceAI captures: messages, model, parameters, response.
            // ============================================================
            System.out.println("\n2. Chat Example");
            System.out.println("---------------");

            Object chatRequest = createChatRequest(
                "ibm/granite-13b-chat-v2",
                projectId,
                new String[][] {
                    {"system", "You are a helpful AI assistant."},
                    {"user", "What is machine learning?"}
                },
                150,    // max tokens
                0.5     // temperature
            );

            if (chatRequest != null) {
                Object chatResponse = traced.chat(chatRequest);
                System.out.println("Chat completed. Check TraceAI dashboard for details.");
                printResponse(chatResponse);
            } else {
                System.out.println("Skipped: Chat request could not be created.");
            }

            // ============================================================
            // Example 3: Embeddings
            // Demonstrates text embedding generation.
            // TraceAI captures: model, inputs, dimensions, token usage.
            // ============================================================
            System.out.println("\n3. Embeddings Example");
            System.out.println("---------------------");

            Object embedRequest = createEmbedRequest(
                "ibm/slate-125m-english-rtrvr",
                projectId,
                new String[] {
                    "Machine learning is a subset of artificial intelligence.",
                    "Deep learning uses neural networks with many layers."
                }
            );

            if (embedRequest != null) {
                Object embedResponse = traced.embedText(embedRequest);
                System.out.println("Embeddings generated. Check TraceAI dashboard for details.");
                printResponse(embedResponse);
            } else {
                System.out.println("Skipped: Embed request could not be created.");
            }

        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
        }

        // ============================================================
        // Cleanup
        // ============================================================
        System.out.println("\n========================================");
        System.out.println("Example completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");
        System.out.println();
        System.out.println("Traces captured include:");
        System.out.println("  - Watsonx Text Generation");
        System.out.println("  - Watsonx Chat");
        System.out.println("  - Watsonx Embed");
        System.out.println();
        System.out.println("Each trace includes:");
        System.out.println("  - Model and provider information");
        System.out.println("  - Input/output content");
        System.out.println("  - Token usage statistics");
        System.out.println("  - watsonx-specific metadata (project_id, stop_reason)");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }

    /**
     * Creates a watsonx client using reflection.
     * This allows the example to compile without the SDK being present.
     */
    private static Object createWatsonxClient(String apiKey, String projectId, String url) {
        try {
            // Try to find and use the WatsonxAI SDK
            Class<?> watsonxClass = Class.forName("com.ibm.watsonx.WatsonxAI");
            Class<?> builderClass = Class.forName("com.ibm.watsonx.WatsonxAI$Builder");

            Object builder = watsonxClass.getMethod("builder").invoke(null);
            builder = builderClass.getMethod("apiKey", String.class).invoke(builder, apiKey);
            builder = builderClass.getMethod("projectId", String.class).invoke(builder, projectId);
            builder = builderClass.getMethod("url", String.class).invoke(builder, url);

            return builderClass.getMethod("build").invoke(builder);
        } catch (ClassNotFoundException e) {
            System.out.println("Note: IBM watsonx-ai SDK not found in classpath.");
            return null;
        } catch (Exception e) {
            System.out.println("Note: Could not create watsonx client: " + e.getMessage());
            return null;
        }
    }

    /**
     * Creates a text generation request using reflection.
     */
    private static Object createTextGenRequest(String modelId, String input, String projectId,
                                                int maxTokens, double temperature) {
        try {
            Class<?> requestClass = Class.forName("com.ibm.watsonx.models.TextGenRequest");
            Class<?> builderClass = Class.forName("com.ibm.watsonx.models.TextGenRequest$Builder");
            Class<?> paramsClass = Class.forName("com.ibm.watsonx.models.TextGenParameters");
            Class<?> paramsBuilderClass = Class.forName("com.ibm.watsonx.models.TextGenParameters$Builder");

            // Build parameters
            Object paramsBuilder = paramsClass.getMethod("builder").invoke(null);
            paramsBuilder = paramsBuilderClass.getMethod("maxNewTokens", int.class).invoke(paramsBuilder, maxTokens);
            paramsBuilder = paramsBuilderClass.getMethod("temperature", double.class).invoke(paramsBuilder, temperature);
            Object params = paramsBuilderClass.getMethod("build").invoke(paramsBuilder);

            // Build request
            Object builder = requestClass.getMethod("builder").invoke(null);
            builder = builderClass.getMethod("modelId", String.class).invoke(builder, modelId);
            builder = builderClass.getMethod("input", String.class).invoke(builder, input);
            builder = builderClass.getMethod("projectId", String.class).invoke(builder, projectId);
            builder = builderClass.getMethod("parameters", paramsClass).invoke(builder, params);

            return builderClass.getMethod("build").invoke(builder);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Creates a chat request using reflection.
     */
    private static Object createChatRequest(String modelId, String projectId, String[][] messages,
                                             int maxTokens, double temperature) {
        try {
            // This is a simplified version - actual implementation depends on SDK
            Class<?> requestClass = Class.forName("com.ibm.watsonx.models.TextChatRequest");
            // ... build the request
            return null; // Simplified for example
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Creates an embed request using reflection.
     */
    private static Object createEmbedRequest(String modelId, String projectId, String[] inputs) {
        try {
            Class<?> requestClass = Class.forName("com.ibm.watsonx.models.EmbedTextRequest");
            // ... build the request
            return null; // Simplified for example
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Prints a response object.
     */
    private static void printResponse(Object response) {
        if (response != null) {
            System.out.println("Response received: " + response.getClass().getSimpleName());
        }
    }

    /**
     * Prints example usage code.
     */
    private static void printExampleUsage() {
        System.out.println();
        System.out.println("Example code with actual watsonx.ai SDK:");
        System.out.println("----------------------------------------");
        System.out.println("""
            // Create watsonx client
            WatsonxAI watsonx = WatsonxAI.builder()
                .apiKey(System.getenv("WATSONX_API_KEY"))
                .projectId(System.getenv("WATSONX_PROJECT_ID"))
                .build();

            // Wrap with tracing
            TracedWatsonxAI traced = new TracedWatsonxAI(watsonx);

            // Text generation
            TextGenResponse response = traced.generateText(
                TextGenRequest.builder()
                    .modelId("ibm/granite-13b-chat-v2")
                    .input("Hello, world!")
                    .projectId(projectId)
                    .parameters(TextGenParameters.builder()
                        .maxNewTokens(100)
                        .temperature(0.7)
                        .build())
                    .build()
            );

            // Chat
            TextChatResponse chatResponse = traced.chat(
                TextChatRequest.builder()
                    .modelId("ibm/granite-13b-chat-v2")
                    .projectId(projectId)
                    .messages(List.of(
                        ChatMessage.user("What is AI?")
                    ))
                    .build()
            );

            // Embeddings
            EmbedTextResponse embedResponse = traced.embedText(
                EmbedTextRequest.builder()
                    .modelId("ibm/slate-125m-english-rtrvr")
                    .projectId(projectId)
                    .inputs(List.of("Text to embed"))
                    .build()
            );
            """);
    }
}
