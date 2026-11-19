package ai.traceai.vertexai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.cloud.vertexai.api.Candidate;
import com.google.cloud.vertexai.api.Content;
import com.google.cloud.vertexai.api.CountTokensResponse;
import com.google.cloud.vertexai.api.FunctionCall;
import com.google.cloud.vertexai.api.GenerateContentResponse;
import com.google.cloud.vertexai.api.Part;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import com.google.cloud.vertexai.generativeai.ResponseStream;
import com.google.protobuf.Struct;
import com.google.protobuf.Value;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * Offline: the real Vertex AI TracedGenerativeModel over a stubbed GenerativeModel (no network, no credentials),
 * one hide flag at a time. Every sentinel the active flag covers must be absent from every exported attribute
 * and event.
 */
class VertexAiContentKeysHideFlagsTest {
    static final String IN_PROMPT = "SENTINEL-VERTEX-IN-PROMPT";
    static final String IN_CONTENT = "SENTINEL-VERTEX-IN-CONTENT";
    static final String IN_STREAM = "SENTINEL-VERTEX-IN-STREAM";
    static final String IN_COUNT = "SENTINEL-VERTEX-IN-COUNT";
    static final String OUT_TEXT = "SENTINEL-VERTEX-OUT-TEXT";
    static final String OUT_TOOL_ARGS = "SENTINEL-VERTEX-OUT-TOOLARGS";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private GenerativeModel model;

    @BeforeEach
    void setup() throws Exception {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        GenerateContentResponse response = GenerateContentResponse.newBuilder()
            .addCandidates(Candidate.newBuilder().setContent(Content.newBuilder().setRole("model")
                .addParts(Part.newBuilder().setText(OUT_TEXT))
                .addParts(Part.newBuilder().setFunctionCall(FunctionCall.newBuilder().setName("lookup")
                    .setArgs(Struct.newBuilder().putFields("q", Value.newBuilder().setStringValue(OUT_TOOL_ARGS).build())))))
                .setFinishReasonValue(1))
            .setUsageMetadata(GenerateContentResponse.UsageMetadata.newBuilder()
                .setPromptTokenCount(3).setCandidatesTokenCount(5).setTotalTokenCount(8))
            .build();
        model = mock(GenerativeModel.class);
        when(model.getModelName()).thenReturn("gemini-1.5-pro");
        when(model.generateContent(anyString())).thenReturn(response);
        when(model.generateContent(anyList())).thenReturn(response);
        when(model.generateContentStream(anyString())).thenReturn(mock(ResponseStream.class));
        when(model.countTokens(anyString())).thenReturn(CountTokensResponse.newBuilder().setTotalTokens(7).build());
    }

    @AfterEach
    void teardown() {
        provider.close();
    }

    private void run(String flag) throws Exception {
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
        TracedGenerativeModel traced = new TracedGenerativeModel(model,
            new FITracer(provider.get("vertex-hide-test"), b.build()));
        traced.generateContent(IN_PROMPT);
        traced.generateContent(List.of(Content.newBuilder().setRole("user")
            .addParts(Part.newBuilder().setText(IN_CONTENT)).build()));
        traced.generateContentStream(IN_STREAM);
        traced.countTokens(IN_COUNT);
        assertThat(exporter.getFinishedSpanItems()).hasSize(4);
    }

    /** Attribute keys (and event names) whose exported value contains the sentinel. */
    private List<String> leakedKeys(String sentinel) {
        List<String> keys = new ArrayList<>();
        for (SpanData span : exporter.getFinishedSpanItems()) {
            span.getAttributes().forEach((k, v) -> { if (String.valueOf(v).contains(sentinel)) keys.add(k.getKey()); });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(sentinel)) keys.add("event:" + e.getName() + "/" + k.getKey());
            }));
        }
        return keys;
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void inputFlagsKeepPromptContentOutOfEverySpan(String flag) throws Exception {
        run(flag);
        for (String s : List.of(IN_PROMPT, IN_CONTENT, IN_STREAM, IN_COUNT)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(leakedKeys(OUT_TEXT)).isNotEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) throws Exception {
        run(flag);
        for (String s : List.of(OUT_TEXT, OUT_TOOL_ARGS)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        assertThat(leakedKeys(IN_PROMPT)).isNotEmpty();
    }
}
