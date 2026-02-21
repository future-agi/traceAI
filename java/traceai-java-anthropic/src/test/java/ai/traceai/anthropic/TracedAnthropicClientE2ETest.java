package ai.traceai.anthropic;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.lang.reflect.Method;

/**
 * E2E tests for TracedAnthropicClient.
 *
 * <p>These tests make REAL API calls and verify that spans are exported
 * to the FI backend for visual verification. They are separate from the
 * mock-based unit tests in TracedAnthropicClientTest.</p>
 *
 * <p>The Anthropic wrapper uses reflection to call the underlying client,
 * so these tests construct the real Anthropic SDK client via reflection
 * to avoid compile-time SDK version coupling.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>ANTHROPIC_API_KEY - API key for Anthropic</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-anthropic-e2e)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedAnthropicClientE2ETest {

    private static FITracer tracer;
    private static TracedAnthropicClient tracedClient;
    private static boolean clientAvailable = false;

    @BeforeAll
    static void setUp() {
        String baseUrl = System.getenv("FI_BASE_URL") != null
            ? System.getenv("FI_BASE_URL")
            : "https://api.futureagi.com";

        if (!TraceAI.isInitialized()) {
            TraceAI.init(TraceConfig.builder()
                .baseUrl(baseUrl)
                .apiKey(System.getenv("FI_API_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                    ? System.getenv("FI_PROJECT_NAME")
                    : "java-anthropic-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        // Build Anthropic client via reflection to support different SDK versions
        try {
            String apiKey = System.getenv("ANTHROPIC_API_KEY");
            if (apiKey != null) {
                // Try: AnthropicClient client = Anthropic.client() or builder pattern
                Class<?> anthropicClass = Class.forName("com.anthropic.client.okhttp.AnthropicOkHttpClient");
                Object builder = anthropicClass.getMethod("builder").invoke(null);
                builder.getClass().getMethod("apiKey", String.class).invoke(builder, apiKey);
                Object client = builder.getClass().getMethod("build").invoke(builder);
                tracedClient = new TracedAnthropicClient(client, tracer);
                clientAvailable = true;
                System.out.println("[E2E] Anthropic client created successfully");
            }
        } catch (Exception e) {
            System.out.println("[E2E] Could not create Anthropic client via reflection: " + e.getMessage());
            // Even without a real client, we can test traced wrapper creation
            tracedClient = new TracedAnthropicClient(new Object(), tracer);
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000); // Allow batch export to flush
    }

    @Test
    @Order(1)
    @EnabledIfEnvironmentVariable(named = "ANTHROPIC_API_KEY", matches = ".+")
    void shouldExportMessageCreationSpan() {
        Assumptions.assumeTrue(clientAvailable, "Anthropic client not available");

        assertThatCode(() -> {
            // Build message params via reflection to stay SDK-version agnostic
            Class<?> paramsClass = Class.forName("com.anthropic.models.MessageCreateParams");
            Object builder = paramsClass.getMethod("builder").invoke(null);

            // Set model
            builder.getClass().getMethod("model", String.class)
                .invoke(builder, "claude-3-haiku-20240307");

            // Set max tokens
            builder.getClass().getMethod("maxTokens", long.class)
                .invoke(builder, 100L);

            // Add message — find the appropriate method
            Class<?> messageParamClass = Class.forName("com.anthropic.models.MessageParam");
            Object msgBuilder = messageParamClass.getMethod("builder").invoke(null);
            msgBuilder.getClass().getMethod("role", String.class).invoke(msgBuilder, "user");
            msgBuilder.getClass().getMethod("content", String.class)
                .invoke(msgBuilder, "Say 'Hello from Java E2E test' and nothing else.");
            Object messageParam = msgBuilder.getClass().getMethod("build").invoke(msgBuilder);

            // Find addMessage method
            for (Method m : builder.getClass().getMethods()) {
                if (m.getName().equals("addMessage") && m.getParameterCount() == 1) {
                    m.invoke(builder, messageParam);
                    break;
                }
            }

            Object params = builder.getClass().getMethod("build").invoke(builder);
            Object result = tracedClient.createMessage(params);

            assertThat(result).isNotNull();
            System.out.println("[E2E] Anthropic message result: " + result);
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(2)
    void shouldCreateTracedClientSuccessfully() {
        // Verify the traced client wrapper can be created and unwrapped
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }

    @Test
    @Order(3)
    void shouldHandleInvalidClientGracefully() {
        // Even with a dummy client, the wrapper should be constructable
        // The error span will still export to the backend
        TracedAnthropicClient dummyClient = new TracedAnthropicClient("not-a-real-client", tracer);
        assertThat(dummyClient).isNotNull();
        assertThat(dummyClient.unwrap()).isEqualTo("not-a-real-client");

        // Calling createMessage on a dummy client will fail, but the error span exports
        try {
            dummyClient.createMessage("dummy-params");
        } catch (RuntimeException e) {
            System.out.println("[E2E] Expected error with dummy client: " + e.getMessage());
            // Error span is still exported to the FI backend
        }
    }
}
