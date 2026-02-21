package ai.traceai.anthropic;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.lang.reflect.Method;

/**
 * E2E tests for TracedAnthropicClient.
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy keys) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-anthropic-e2e)</li>
 *   <li>ANTHROPIC_API_KEY - API key for Anthropic</li>
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
                .secretKey(System.getenv("FI_SECRET_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                    ? System.getenv("FI_PROJECT_NAME")
                    : "java-anthropic-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String apiKey = System.getenv("ANTHROPIC_API_KEY") != null
            ? System.getenv("ANTHROPIC_API_KEY")
            : "dummy-key-for-e2e";

        try {
            Class<?> anthropicClass = Class.forName("com.anthropic.client.okhttp.AnthropicOkHttpClient");
            Object builder = anthropicClass.getMethod("builder").invoke(null);
            builder.getClass().getMethod("apiKey", String.class).invoke(builder, apiKey);
            Object client = builder.getClass().getMethod("build").invoke(builder);
            tracedClient = new TracedAnthropicClient(client, tracer);
            clientAvailable = true;
            System.out.println("[E2E] Anthropic client created successfully");
        } catch (Exception e) {
            System.out.println("[E2E] Could not create Anthropic client via reflection: " + e.getMessage());
            tracedClient = new TracedAnthropicClient(new Object(), tracer);
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportMessageCreationSpan() {
        Assumptions.assumeTrue(clientAvailable, "Anthropic client not available");

        try {
            Class<?> paramsClass = Class.forName("com.anthropic.models.MessageCreateParams");
            Object builder = paramsClass.getMethod("builder").invoke(null);

            builder.getClass().getMethod("model", String.class)
                .invoke(builder, "claude-3-haiku-20240307");
            builder.getClass().getMethod("maxTokens", long.class)
                .invoke(builder, 100L);

            Class<?> messageParamClass = Class.forName("com.anthropic.models.MessageParam");
            Object msgBuilder = messageParamClass.getMethod("builder").invoke(null);
            msgBuilder.getClass().getMethod("role", String.class).invoke(msgBuilder, "user");
            msgBuilder.getClass().getMethod("content", String.class)
                .invoke(msgBuilder, "Say 'Hello from Java E2E test' and nothing else.");
            Object messageParam = msgBuilder.getClass().getMethod("build").invoke(msgBuilder);

            for (Method m : builder.getClass().getMethods()) {
                if (m.getName().equals("addMessage") && m.getParameterCount() == 1) {
                    m.invoke(builder, messageParam);
                    break;
                }
            }

            Object params = builder.getClass().getMethod("build").invoke(builder);
            Object result = tracedClient.createMessage(params);
            System.out.println("[E2E] Anthropic message result: " + result);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }

    @Test
    @Order(3)
    void shouldHandleInvalidClientGracefully() {
        TracedAnthropicClient dummyClient = new TracedAnthropicClient("not-a-real-client", tracer);
        assertThat(dummyClient).isNotNull();
        assertThat(dummyClient.unwrap()).isEqualTo("not-a-real-client");

        try {
            dummyClient.createMessage("dummy-params");
        } catch (RuntimeException e) {
            System.out.println("[E2E] Expected error with dummy client: " + e.getMessage());
        }
    }
}
