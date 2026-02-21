package ai.traceai.bedrock;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

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
 * <p>These tests make REAL API calls to AWS Bedrock and verify that spans
 * are exported to the FI backend for visual verification. They are separate
 * from the mock-based unit tests in TracedBedrockRuntimeClientTest.</p>
 *
 * <p>Required environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>AWS_ACCESS_KEY_ID - AWS access key</li>
 *   <li>AWS_SECRET_ACCESS_KEY - AWS secret access key</li>
 * </ul>
 *
 * <p>Optional environment variables:</p>
 * <ul>
 *   <li>FI_BASE_URL - FI backend URL (default: https://api.futureagi.com)</li>
 *   <li>FI_PROJECT_NAME - Project name (default: java-bedrock-e2e)</li>
 *   <li>AWS_REGION - AWS region (default: us-east-1)</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "AWS_ACCESS_KEY_ID", matches = ".+")
@EnabledIfEnvironmentVariable(named = "AWS_SECRET_ACCESS_KEY", matches = ".+")
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

        BedrockRuntimeClient client = BedrockRuntimeClient.builder()
            .region(Region.of(region))
            .credentialsProvider(StaticCredentialsProvider.create(
                AwsBasicCredentials.create(
                    System.getenv("AWS_ACCESS_KEY_ID"),
                    System.getenv("AWS_SECRET_ACCESS_KEY")
                )
            ))
            .build();

        tracedClient = new TracedBedrockRuntimeClient(client, tracer);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        Thread.sleep(3000); // Allow batch export to flush
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

        assertThatCode(() -> {
            ConverseResponse response = tracedClient.converse(request);
            assertThat(response).isNotNull();
            assertThat(response.output()).isNotNull();
            assertThat(response.output().message()).isNotNull();

            String outputText = response.output().message().content().stream()
                .filter(block -> block.text() != null)
                .map(ContentBlock::text)
                .findFirst()
                .orElse("(no text)");

            System.out.println("[E2E] Bedrock converse response: " + outputText);

            if (response.usage() != null) {
                System.out.println("[E2E] Bedrock tokens - input: " +
                    response.usage().inputTokens() + ", output: " +
                    response.usage().outputTokens());
            }
        }).doesNotThrowAnyException();
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

        assertThatCode(() -> {
            ConverseResponse response = tracedClient.converse(request);
            assertThat(response).isNotNull();
            System.out.println("[E2E] Bedrock converse with system prompt: " +
                response.output().message().content().get(0).text());
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(3)
    void shouldExportInvokeModelSpan() {
        // Use the Anthropic Messages API format via invokeModel
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

        assertThatCode(() -> {
            InvokeModelResponse response = tracedClient.invokeModel(request);
            assertThat(response).isNotNull();
            String responseBody = response.body().asUtf8String();
            assertThat(responseBody).isNotEmpty();
            System.out.println("[E2E] Bedrock invokeModel response: " + responseBody);
        }).doesNotThrowAnyException();
    }

    @Test
    @Order(4)
    void shouldCreateTracedClientSuccessfully() {
        assertThat(tracedClient).isNotNull();
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
