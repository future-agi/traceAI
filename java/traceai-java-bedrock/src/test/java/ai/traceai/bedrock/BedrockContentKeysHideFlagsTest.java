package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.document.Document;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.lang.reflect.Proxy;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Offline: the real Bedrock instrumentation over a stubbed runtime client, with each hide flag set on its
 * own. Every sentinel the active flag covers must be absent from every exported attribute and event.
 */
class BedrockContentKeysHideFlagsTest {
    static final String IN_TEXT = "SENTINEL-BEDROCK-IN-TEXT";
    static final String IN_SYSTEM = "SENTINEL-BEDROCK-IN-SYSTEM";
    static final String IN_TOOL_RESULT = "SENTINEL-BEDROCK-IN-TOOLRESULT";
    static final String IN_TOOL_DEF = "SENTINEL-BEDROCK-IN-TOOLDEF";
    static final String IN_INVOKE = "SENTINEL-BEDROCK-IN-INVOKE";
    static final String OUT_TEXT = "SENTINEL-BEDROCK-OUT-TEXT";
    static final String OUT_TOOL_ARGS = "SENTINEL-BEDROCK-OUT-TOOLARGS";
    static final String OUT_INVOKE = "SENTINEL-BEDROCK-OUT-INVOKE";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private BedrockRuntimeClient stub;

    @BeforeEach
    void setup() {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        Message answer = Message.builder().role("assistant").content(
            ContentBlock.fromText(OUT_TEXT),
            ContentBlock.fromToolUse(ToolUseBlock.builder().toolUseId("t1").name("lookup")
                .input(Document.fromMap(Map.of("q", Document.fromString(OUT_TOOL_ARGS)))).build())).build();
        ConverseResponse converseResponse = ConverseResponse.builder()
            .output(ConverseOutput.builder().message(answer).build()).stopReason("end_turn")
            .usage(TokenUsage.builder().inputTokens(3).outputTokens(5).totalTokens(8).build()).build();
        InvokeModelResponse invokeResponse = (InvokeModelResponse) InvokeModelResponse.builder()
            .body(SdkBytes.fromUtf8String("{\"results\":[{\"outputText\":\"" + OUT_INVOKE + "\"}]}")).build();
        stub = (BedrockRuntimeClient) Proxy.newProxyInstance(
            BedrockRuntimeClient.class.getClassLoader(), new Class<?>[] {BedrockRuntimeClient.class},
            (proxy, method, args) -> {
                switch (method.getName()) {
                    case "converse":
                        return converseResponse;
                    case "invokeModel":
                        return invokeResponse;
                    case "close":
                        return null;
                    case "toString":
                        return "stubBedrockRuntimeClient";
                    case "hashCode":
                        return System.identityHashCode(proxy);
                    case "equals":
                        return proxy == args[0];
                    default:
                        throw new UnsupportedOperationException(method.getName());
                }
            });
    }

    @AfterEach
    void teardown() {
        provider.close();
    }

    private FITracer tracer(String flag) {
        TraceConfig.Builder b = TraceConfig.builder().apiKey("offline").secretKey("offline").projectName("offline")
            .hideInputs(false).hideOutputs(false).hideInputMessages(false).hideOutputMessages(false)
            .hideErrorDetails(false);
        switch (flag) {
            case "hideInputs" -> b.hideInputs(true);
            case "hideOutputs" -> b.hideOutputs(true);
            case "hideInputMessages" -> b.hideInputMessages(true);
            case "hideOutputMessages" -> b.hideOutputMessages(true);
            default -> throw new IllegalArgumentException(flag);
        }
        return new FITracer(provider.get("bedrock-hide-test"), b.build());
    }

    private String exported() {
        StringBuilder all = new StringBuilder();
        for (SpanData span : exporter.getFinishedSpanItems()) {
            all.append(span.getName()).append('\n').append(span.getAttributes()).append('\n');
            span.getEvents().forEach(e -> all.append(e.getName()).append(e.getAttributes()).append('\n'));
        }
        return all.toString();
    }

    /** Attribute keys (and event names) whose exported value contains the sentinel. */
    private List<String> leakedKeys(String sentinel) {
        List<String> keys = new java.util.ArrayList<>();
        for (SpanData span : exporter.getFinishedSpanItems()) {
            span.getAttributes().forEach((k, v) -> { if (String.valueOf(v).contains(sentinel)) keys.add(k.getKey()); });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(sentinel)) keys.add("event:" + e.getName() + "/" + k.getKey());
            }));
        }
        return keys;
    }

    private static ConverseRequest converseRequest() {
        return ConverseRequest.builder().modelId("anthropic.claude-3-haiku-20240307-v1:0")
            .system(SystemContentBlock.builder().text(IN_SYSTEM).build())
            .messages(
                Message.builder().role("user").content(ContentBlock.fromText(IN_TEXT)).build(),
                Message.builder().role("user").content(ContentBlock.fromToolResult(ToolResultBlock.builder()
                    .toolUseId("t0").content(ToolResultContentBlock.fromText(IN_TOOL_RESULT)).build())).build())
            .toolConfig(ToolConfiguration.builder().tools(Tool.fromToolSpec(ToolSpecification.builder()
                .name("lookup").description(IN_TOOL_DEF)
                .inputSchema(ToolInputSchema.fromJson(Document.fromMap(Map.of("type", Document.fromString("object")))))
                .build())).build())
            .build();
    }

    private void invoke(FITracer tracer) {
        TracedBedrockRuntimeClient client = new TracedBedrockRuntimeClient(stub, tracer);
        client.converse(converseRequest());
        client.invokeModel(InvokeModelRequest.builder().modelId("amazon.titan-text-express-v1")
            .contentType("application/json")
            .body(SdkBytes.fromUtf8String("{\"inputText\":\"" + IN_INVOKE + "\"}")).build());
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void inputFlagsKeepPromptContentOutOfEverySpan(String flag) {
        invoke(tracer(flag));
        String out = exported();
        for (String s : List.of(IN_TEXT, IN_SYSTEM, IN_TOOL_RESULT, IN_INVOKE)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        if (flag.equals("hideInputs")) assertThat(leakedKeys(IN_TOOL_DEF)).as("tool definition under hideInputs").isEmpty();
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(out).contains(OUT_TEXT).contains(OUT_INVOKE);
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) {
        invoke(tracer(flag));
        String out = exported();
        for (String s : List.of(OUT_TEXT, OUT_TOOL_ARGS, OUT_INVOKE)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        assertThat(out).contains(IN_TEXT).contains(IN_INVOKE);
    }
}
