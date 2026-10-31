package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.gson.JsonParser;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.document.Document;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.lang.reflect.Proxy;
import java.util.Map;

import static org.assertj.core.api.Assertions.*;

class BedrockTracingTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @ValueSource(strings = {"amazon.titan-text-express-v1", "us.amazon.titan-text-express-v1",
        "eu.amazon.titan-text-express-v1", "apac.amazon.titan-text-express-v1",
        "global.amazon.titan-text-express-v1",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.titan-text-express-v1"})
    void profileIdsSelectTheModelParserWithoutChangingTheRecordedModel(String model) {
        InvokeModelResponse response = InvokeModelResponse.builder().body(SdkBytes.fromUtf8String(
            "{\"results\":[{\"outputText\":\"answer\"}]}")).build();
        var client = new TracedBedrockRuntimeClient(stub(response), tracer(false));
        var request = InvokeModelRequest.builder().modelId(model)
            .body(SdkBytes.fromUtf8String("{\"inputText\":\"question\"}")).build();
        assertThat(client.invokeModel(request)).isSameAs(response);
        SpanData span = otel.getSpans().get(0);
        assertThat(value(span, SemanticConventions.INPUT_VALUE)).isEqualTo("question");
        assertThat(value(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("answer");
        assertThat(value(span, SemanticConventions.LLM_REQUEST_MODEL)).isEqualTo(model);
    }

    @Test
    void hostingProviderDoesNotOverwriteModelVendor() {
        new TracedBedrockRuntimeClient(stub(response()), tracer(false)).converse(request());
        SpanData span = otel.getSpans().get(0);
        assertThat(value(span, SemanticConventions.LLM_PROVIDER)).isEqualTo("aws.bedrock");
        assertThat(value(span, "gen_ai.bedrock.model_provider")).isEqualTo("anthropic");
    }

    @Test
    void conversePreservesToolsAndMultimodalBlocksWithoutBinaryPayloads() {
        new TracedBedrockRuntimeClient(stub(response()), tracer(false)).converse(request());
        SpanData span = otel.getSpans().get(0);
        String input = value(span, SemanticConventions.LLM_INPUT_MESSAGES);
        assertThat(input).contains("toolUse", "lookup", "toolResult", "tool-1", "image");
        assertThat(JsonParser.parseString(input).getAsJsonArray().get(1).getAsJsonObject()
            .getAsJsonArray("content").size()).isEqualTo(3);
        assertThat(value(span, SemanticConventions.LLM_OUTPUT_MESSAGES)).contains("toolUse", "tool-2", "lookup");
        assertThat(value(span, SemanticConventions.GEN_AI_TOOL_DEFINITIONS)).contains("lookup", "object");
        assertThat(span.getAttributes().toString()).doesNotContain("private-binary", "cHJpdmF0ZS1iaW5hcnk=");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
            .isEqualTo(8);
    }

    @Test
    void invokeMessagesContentArraysAreNotDropped() {
        var client = new TracedBedrockRuntimeClient(stub(InvokeModelResponse.builder().body(SdkBytes.fromUtf8String(
            "{\"content\":[{\"type\":\"text\",\"text\":\"first\"},{\"type\":\"text\",\"text\":\"second\"}],"
            + "\"usage\":{\"input_tokens\":3,\"output_tokens\":5}}")).build()), tracer(false));
        client.invokeModel(InvokeModelRequest.builder().modelId("us.anthropic.claude-3-haiku-20240307-v1:0")
            .body(SdkBytes.fromUtf8String("{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"question\"}]}]}"))
            .build());
        SpanData span = otel.getSpans().get(0);
        assertThat(value(span, SemanticConventions.LLM_INPUT_MESSAGES)).contains("question");
        assertThat(value(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("firstsecond");
        assertThat(value(span, SemanticConventions.GEN_AI_OPERATION_NAME)).isEqualTo("chat");
    }

    @Test
    void hiddenContentDoesNotLeakThroughToolsOrRawAttributes() {
        new TracedBedrockRuntimeClient(stub(response()), tracer(true)).converse(request());
        SpanData span = otel.getSpans().get(0);
        assertThat(span.getAttributes().toString()).doesNotContain("question", "lookup", "tool-1", "private-binary");
        assertThat(value(span, SemanticConventions.RAW_INPUT)).isNull();
        assertThat(value(span, SemanticConventions.RAW_OUTPUT)).isNull();
        assertThat(value(span, SemanticConventions.GEN_AI_TOOL_DEFINITIONS)).isNull();
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(8);
    }

    @ParameterizedTest
    @ValueSource(strings = {"custom-model", "not-anthropic.model", "other.anthropic.model",
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/anthropic.opaque",
        "arn:aws:other:us-east-1:123456789012:foundation-model/anthropic.model"})
    void opaqueProfilesDoNotInventAVendor(String model) {
        assertThat(BedrockTracing.modelProvider(model)).isNull();
    }

    @Test
    void foundationModelArnAndNullIdsAreSafe() {
        assertThat(BedrockTracing.modelProvider("arn:aws:bedrock:us-east-1::foundation-model/anthropic.model"))
            .isEqualTo("anthropic");
        assertThat(BedrockTracing.modelProvider(null)).isNull();
    }

    @Test
    void textOnlyMessagesKeepTheirExistingShape() {
        ConverseRequest request = ConverseRequest.builder().modelId("model")
            .messages(Message.builder().role(ConversationRole.USER)
                .content(ContentBlock.builder().text("hello").build()).build()).build();
        new TracedBedrockRuntimeClient(stub(response()), tracer(false)).converse(request);
        assertThat(value(otel.getSpans().get(0), SemanticConventions.LLM_INPUT_MESSAGES))
            .isEqualTo("[{\"role\":\"user\",\"content\":\"hello\"}]");
    }

    @Test
    void malformedPayloadsDoNotChangeApplicationResults() {
        InvokeModelResponse response = InvokeModelResponse.builder().body(SdkBytes.fromUtf8String("not json")).build();
        var client = new TracedBedrockRuntimeClient(stub(response), tracer(false));
        assertThat(client.invokeModel(InvokeModelRequest.builder().modelId("unknown")
            .body(SdkBytes.fromUtf8String("not json")).build())).isSameAs(response);
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(otel.getSpans().get(0).getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.OK);
    }

    @Test
    void failuresPreserveIdentityAndEndTheFacadeSpan() {
        RuntimeException error = new IllegalStateException("private-error");
        var client = new TracedBedrockRuntimeClient(stub(error), tracer(true));
        assertThatThrownBy(() -> client.converse(request())).isSameAs(error);
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(otel.getSpans().get(0).getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
        assertThat(otel.getSpans().get(0).toString()).doesNotContain("private-error");
    }

    @ParameterizedTest
    @ValueSource(ints = {0, 1, 2, 3})
    void messageHidingFlagsAreIndependent(int flags) {
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("flags"), TraceConfig.builder()
            .apiKey("offline").secretKey("offline").projectName("offline")
            .hideInputMessages((flags & 1) != 0).hideOutputMessages((flags & 2) != 0).build());
        new TracedBedrockRuntimeClient(stub(response()), tracer).converse(request());
        SpanData span = otel.getSpans().get(0);
        assertThat(value(span, SemanticConventions.LLM_INPUT_MESSAGES) == null).isEqualTo((flags & 1) != 0);
        assertThat(value(span, SemanticConventions.LLM_OUTPUT_MESSAGES) == null).isEqualTo((flags & 2) != 0);
    }

    @Test
    void inlineBinaryProviderContentIsRedacted() {
        var client = new TracedBedrockRuntimeClient(stub(InvokeModelResponse.builder()
            .body(SdkBytes.fromUtf8String("{\"completion\":\"answer\"}")).build()), tracer(false));
        client.invokeModel(InvokeModelRequest.builder().modelId("anthropic.model")
            .body(SdkBytes.fromUtf8String("{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"image\","
                + "\"source\":{\"type\":\"base64\",\"data\":\"private-binary\"}}]}]}")).build());
        assertThat(otel.getSpans().get(0).getAttributes().toString()).doesNotContain("private-binary").contains("omitted");
    }

    static ConverseRequest request() {
        return ConverseRequest.builder().modelId("us.anthropic.claude-3-haiku-20240307-v1:0")
            .system(SystemContentBlock.builder().text("system instruction").build())
            .messages(Message.builder().role(ConversationRole.USER).content(
                ContentBlock.builder().text("question").build(),
                ContentBlock.builder().image(ImageBlock.builder().format(ImageFormat.PNG)
                    .source(ImageSource.builder().bytes(SdkBytes.fromUtf8String("private-binary")).build()).build()).build(),
                ContentBlock.builder().toolResult(ToolResultBlock.builder().toolUseId("tool-1")
                    .content(ToolResultContentBlock.builder().json(Document.fromMap(Map.of("answer", Document.fromString("found")))).build())
                    .build()).build()).build(),
                Message.builder().role(ConversationRole.ASSISTANT).content(tool("tool-1")).build())
            .toolConfig(ToolConfiguration.builder().tools(Tool.builder().toolSpec(ToolSpecification.builder()
                .name("lookup").description("Find a record").inputSchema(ToolInputSchema.builder()
                    .json(Document.fromMap(Map.of("type", Document.fromString("object")))).build()).build()).build()).build())
            .inferenceConfig(InferenceConfiguration.builder().maxTokens(64).temperature(0.2f).build()).build();
    }

    static ContentBlock tool(String id) {
        return ContentBlock.builder().toolUse(ToolUseBlock.builder().toolUseId(id).name("lookup")
            .input(Document.fromMap(Map.of("query", Document.fromString("question")))).build()).build();
    }

    static ConverseResponse response() {
        return ConverseResponse.builder().output(ConverseOutput.builder().message(Message.builder()
            .role(ConversationRole.ASSISTANT).content(tool("tool-2")).build()).build())
            .stopReason(StopReason.TOOL_USE).usage(TokenUsage.builder().inputTokens(3).outputTokens(5).totalTokens(8).build()).build();
    }

    static BedrockRuntimeClient stub(Object response) {
        return (BedrockRuntimeClient) Proxy.newProxyInstance(BedrockRuntimeClient.class.getClassLoader(),
            new Class<?>[] {BedrockRuntimeClient.class}, (proxy, method, args) -> {
                if (method.getName().equals("invokeModel") || method.getName().equals("converse")) {
                    if (response instanceof RuntimeException) throw (RuntimeException) response;
                    return response;
                }
                if (method.getName().equals("close")) return null;
                if (method.getName().equals("serviceName")) return "bedrock-runtime";
                throw new UnsupportedOperationException(method.getName());
            });
    }

    static FITracer tracer(boolean hide) {
        return new FITracer(otel.getOpenTelemetry().getTracer("bedrock-test"), TraceConfig.builder()
            .apiKey("offline").secretKey("offline").projectName("offline")
            .hideInputs(hide).hideOutputs(hide).hideErrorDetails(true).build());
    }

    static String value(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }
}
