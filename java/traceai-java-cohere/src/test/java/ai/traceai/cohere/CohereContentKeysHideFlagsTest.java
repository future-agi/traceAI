package ai.traceai.cohere;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.cohere.api.Cohere;
import com.cohere.api.core.ClientOptions;
import com.cohere.api.requests.ChatRequest;
import com.cohere.api.requests.EmbedRequest;
import com.cohere.api.requests.RerankRequest;
import com.cohere.api.types.ChatMessage;
import com.cohere.api.types.ChatToolMessage;
import com.cohere.api.types.EmbedFloatsResponse;
import com.cohere.api.types.EmbedResponse;
import com.cohere.api.types.Message;
import com.cohere.api.types.NonStreamedChatResponse;
import com.cohere.api.types.RerankRequestDocumentsItem;
import com.cohere.api.types.RerankResponse;
import com.cohere.api.types.RerankResponseResultsItem;
import com.cohere.api.types.ToolCall;
import com.cohere.api.types.ToolResult;
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
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Offline: the real TracedCohereClient over a stubbed Cohere client, one hide flag at a time. Every sentinel
 * the active flag covers must be absent from every exported span attribute and event.
 */
class CohereContentKeysHideFlagsTest {
    static final String IN_MESSAGE = "SENTINEL-COHERE-IN-MESSAGE";
    static final String IN_PREAMBLE = "SENTINEL-COHERE-IN-PREAMBLE";
    static final String IN_HISTORY = "SENTINEL-COHERE-IN-HISTORY";
    static final String IN_TOOL_RESULT = "SENTINEL-COHERE-IN-TOOLRESULT";
    static final String IN_EMBED_TEXT = "SENTINEL-COHERE-IN-EMBEDTEXT";
    static final String IN_QUERY = "SENTINEL-COHERE-IN-QUERY";
    static final String IN_DOCUMENT = "SENTINEL-COHERE-IN-DOCUMENT";
    static final String OUT_TEXT = "SENTINEL-COHERE-OUT-TEXT";
    static final String OUT_TOOL_ARGS = "SENTINEL-COHERE-OUT-TOOLARGS";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;

    @BeforeEach
    void setup() {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    }

    @AfterEach
    void teardown() {
        provider.close();
    }

    private static class StubCohere extends Cohere {
        StubCohere() { super(ClientOptions.builder().build()); }

        @Override
        public NonStreamedChatResponse chat(ChatRequest request) {
            // the v1 chat response echoes the conversation, including the user message
            return NonStreamedChatResponse.builder().text(OUT_TEXT)
                .chatHistory(List.of(
                    Message.user(ChatMessage.builder().message(IN_MESSAGE).build()),
                    Message.user(ChatMessage.builder().message(IN_HISTORY).build())))
                .toolCalls(List.of(ToolCall.builder().name("lookup").parameters(Map.of("q", OUT_TOOL_ARGS)).build()))
                .build();
        }

        @Override
        public EmbedResponse embed(EmbedRequest request) {
            return EmbedResponse.embeddingsFloats(EmbedFloatsResponse.builder()
                .id("embed-1").embeddings(List.of(List.of(0.1, 0.2))).texts(List.of()).build());
        }

        @Override
        public RerankResponse rerank(RerankRequest request) {
            return RerankResponse.builder()
                .results(List.of(RerankResponseResultsItem.builder().index(0).relevanceScore(0.9f).build())).build();
        }
    }

    private void run(String flag) {
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
        TracedCohereClient client = new TracedCohereClient(new StubCohere(),
            new FITracer(provider.get("cohere-hide-test"), b.build()));
        client.chat(ChatRequest.builder().message(IN_MESSAGE).preamble(IN_PREAMBLE).model("command-r")
            .chatHistory(List.of(
                Message.user(ChatMessage.builder().message(IN_HISTORY).build()),
                Message.tool(ChatToolMessage.builder().toolResults(List.of(ToolResult.builder()
                    .call(ToolCall.builder().name("lookup").parameters(Map.of("q", "x")).build())
                    .outputs(List.of(Map.of("r", IN_TOOL_RESULT))).build())).build())))
            .build());
        client.embed(EmbedRequest.builder().texts(List.of(IN_EMBED_TEXT)).model("embed-english-v3.0").build());
        client.rerank(RerankRequest.builder().query(IN_QUERY)
            .documents(List.of(RerankRequestDocumentsItem.of(IN_DOCUMENT))).topN(1).model("rerank-v3").build());
        assertThat(exporter.getFinishedSpanItems()).hasSize(3);
    }

    /** Attribute keys (and event names) whose exported value contains the sentinel. */
    private List<String> exportedKeys(String sentinel) {
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
    void inputFlagsKeepPromptContentOutOfEverySpan(String flag) {
        run(flag);
        for (String s : List.of(IN_MESSAGE, IN_PREAMBLE, IN_HISTORY, IN_TOOL_RESULT, IN_EMBED_TEXT, IN_QUERY, IN_DOCUMENT)) {
            List<String> exported = exportedKeys(s);
            // gen_ai.reranker.query is on Python's hide_inputs list only, so hideInputMessages keeps it by design
            if (flag.equals("hideInputMessages")) exported.remove("gen_ai.reranker.query");
            assertThat(exported).as("%s exported under %s", s, flag).isEmpty();
        }
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(exportedKeys(OUT_TEXT)).isNotEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) {
        run(flag);
        for (String s : List.of(OUT_TEXT, OUT_TOOL_ARGS)) {
            assertThat(exportedKeys(s)).as("%s exported under %s", s, flag).isEmpty();
        }
        assertThat(exportedKeys(IN_MESSAGE)).isNotEmpty();
    }

    /**
     * tool_call.function.arguments is on Python's input-side list and Java masks it, wherever it is nested, under
     * every one of the four flags (ContentKeys javadoc). Only the nested argument attributes are inspected:
     * raw.output legitimately carries the whole response unless an output flag hides it.
     */
    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideOutputs", "hideInputMessages", "hideOutputMessages"})
    void toolCallArgumentAttributesAreMaskedUnderEveryFlag(String flag) {
        run(flag);
        List<String> exported = new ArrayList<>();
        for (SpanData span : exporter.getFinishedSpanItems()) {
            span.getAttributes().forEach((k, v) -> {
                if (k.getKey().contains("tool_call.function.arguments") && String.valueOf(v).contains(OUT_TOOL_ARGS)) {
                    exported.add(k.getKey());
                }
            });
        }
        assertThat(exported).as("tool-call arguments under %s", flag).isEmpty();
    }

    /** Control: with no hide flag the echoed history is part of the raw output. */
    @org.junit.jupiter.api.Test
    void withNoFlagTheEchoedHistoryIsInTheRawOutput() {
        TracedCohereClient client = new TracedCohereClient(new StubCohere(), new FITracer(provider.get("cohere-hide-test"),
            TraceConfig.builder().apiKey("offline").secretKey("offline").projectName("offline")
                .hideInputs(false).hideOutputs(false).hideInputMessages(false).hideOutputMessages(false).build()));
        client.chat(ChatRequest.builder().message(IN_MESSAGE).model("command-r").build());
        assertThat(exportedKeys(IN_HISTORY)).contains("fi.raw_output");
        assertThat(exportedKeys(OUT_TEXT)).contains("fi.raw_output");
    }
}
