package ai.traceai.bedrock;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.util.List;

/**
 * E2E tests for TracedBedrockRuntimeClient.
 *
 * <p>These tests export spans to the FI backend. Even error spans (from
 * dummy credentials) appear in the UI for visual verification.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-bedrock-e2e)</li>
 *   <li>AWS_ACCESS_KEY_ID - AWS access key</li>
 *   <li>AWS_SECRET_ACCESS_KEY - AWS secret access key</li>
 *   <li>AWS_REGION - AWS region (default: us-east-1)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedBedrockRuntimeClientE2ETest {

    private static FITracer tracer;
    private static TracedBedrockRuntimeClient tracedClient;

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
                    : "java-bedrock-e2e")
                .enableConsoleExporter(true)
                .build());
        }

        tracer = TraceAI.getTracer();

        String region = System.getenv("AWS_REGION") != null
            ? System.getenv("AWS_REGION")
            : "us-east-1";

        String accessKeyId = System.getenv("AWS_ACCESS_KEY_ID") != null
            ? System.getenv("AWS_ACCESS_KEY_ID")
            : "dummy-access-key";

        String secretAccessKey = System.getenv("AWS_SECRET_ACCESS_KEY") != null
            ? System.getenv("AWS_SECRET_ACCESS_KEY")
            : "dummy-secret-key";

        BedrockRuntimeClient client = BedrockRuntimeClient.builder()
            .region(Region.of(region))
            .credentialsProvider(StaticCredentialsProvider.create(
                AwsBasicCredentials.create(accessKeyId, secretAccessKey)
            ))
            .build();

        tracedClient = new TracedBedrockRuntimeClient(client, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldExportConverseSpan() {
        ConverseRequest request = ConverseRequest.builder()
            .modelId("anthropic.claude-3-haiku-20240307-v1:0")
            .messages(
                Message.builder()
                    .role(ConversationRole.USER)
                    .content(
                        ContentBlock.builder()
                            .text("Say 'Hello from Java E2E test' and nothing else.")
                            .build()
                    )
                    .build()
            )
            .inferenceConfig(InferenceConfiguration.builder()
                .maxTokens(100)
                .temperature(0.0F)
                .build())
            .build();

        try {
            ConverseResponse response = tracedClient.converse(request);
            String outputText = response.output().message().content().stream()
                .filter(block -> block.text() != null)
                .map(ContentBlock::text)
                .findFirst()
                .orElse("(no text)");
            System.out.println("[E2E] Bedrock converse response: " + outputText);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(2)
    void shouldExportConverseWithSystemPromptSpan() {
        ConverseRequest request = ConverseRequest.builder()
            .modelId("anthropic.claude-3-haiku-20240307-v1:0")
            .system(
                SystemContentBlock.builder()
                    .text("You are a helpful assistant that always replies in exactly 3 words.")
                    .build()
            )
            .messages(
                Message.builder()
                    .role(ConversationRole.USER)
                    .content(
                        ContentBlock.builder()
                            .text("What is the meaning of life?")
                            .build()
                    )
                    .build()
            )
            .inferenceConfig(InferenceConfiguration.builder()
                .maxTokens(50)
                .build())
            .build();

        try {
            ConverseResponse response = tracedClient.converse(request);
            System.out.println("[E2E] Bedrock converse with system prompt: " +
                response.output().message().content().get(0).text());
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldExportInvokeModelSpan() {
        String requestBody = "{"
            + "\"anthropic_version\":\"bedrock-2023-05-31\","
            + "\"max_tokens\":100,"
            + "\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one word.\"}]"
            + "}";

        InvokeModelRequest request = InvokeModelRequest.builder()
            .modelId("anthropic.claude-3-haiku-20240307-v1:0")
            .contentType("application/json")
            .accept("application/json")
            .body(SdkBytes.fromUtf8String(requestBody))
            .build();

        try {
            InvokeModelResponse response = tracedClient.invokeModel(request);
            String responseBody = response.body().asUtf8String();
            System.out.println("[E2E] Bedrock invokeModel response: " + responseBody);
        } catch (Exception e) {
            System.out.println("[E2E] Error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
