package ai.traceai.cohere;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.cohere.api.Cohere;
import com.cohere.api.core.ClientOptions;
import com.cohere.api.types.EmbedResponse;
import com.cohere.api.requests.ChatRequest;
import com.cohere.api.requests.EmbedRequest;
import com.cohere.api.requests.RerankRequest;
import com.cohere.api.types.NonStreamedChatResponse;
import com.cohere.api.types.RerankRequestDocumentsItem;
import com.cohere.api.types.RerankResponse;
import com.cohere.api.types.RerankResponseResultsItem;
import com.cohere.api.types.ToolCall;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.*;

class TracedCohereClientPrivacyTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @ValueSource(ints = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
    void shouldGateChatAndRerankContentButKeepOperationalAttributes(int flags) {
        boolean inputs = (flags & 1) != 0;
        boolean outputs = (flags & 2) != 0;
        boolean inputMessages = (flags & 4) != 0;
        boolean outputMessages = (flags & 8) != 0;
        TraceConfig config = TraceConfig.builder().hideInputs(inputs).hideOutputs(outputs)
            .hideInputMessages(inputMessages).hideOutputMessages(outputMessages).build();
        FakeCohere client = new FakeCohere();
        TracedCohereClient traced = traced(client, config);
        ChatRequest chat = ChatRequest.builder().message("private-input").preamble("private-preamble")
            .model("test-model").build();
        NonStreamedChatResponse response = NonStreamedChatResponse.builder().text("private-output")
            .toolCalls(List.of(ToolCall.builder().name("private-tool")
                .parameters(Map.of("argument", "private-argument")).build())).build();
        client.chatResponse = response;
        assertThat(traced.chat(chat)).isSameAs(response);
        assertThat(client.chatRequest).isSameAs(chat);

        SpanData chatSpan = otel.getSpans().get(0);
        assertThat(value(chatSpan, "cohere.preamble") == null).isEqualTo(inputs || inputMessages);
        assertThat(value(chatSpan, SemanticConventions.INPUT_VALUE) == null).isEqualTo(inputs);
        assertThat(value(chatSpan, SemanticConventions.LLM_INPUT_MESSAGES) == null)
            .isEqualTo(inputs || inputMessages);
        assertThat(value(chatSpan, SemanticConventions.RAW_INPUT) == null).isEqualTo(inputs);
        assertThat(value(chatSpan, SemanticConventions.OUTPUT_VALUE) == null).isEqualTo(outputs);
        assertThat(value(chatSpan, SemanticConventions.LLM_OUTPUT_MESSAGES) == null)
            .isEqualTo(outputs || outputMessages);
        assertThat(value(chatSpan, SemanticConventions.RAW_OUTPUT) == null).isEqualTo(outputs);
        String toolPrefix = SemanticConventions.MESSAGE_TOOL_CALLS + ".0.";
        assertThat(value(chatSpan, toolPrefix + SemanticConventions.TOOL_CALL_FUNCTION_NAME) == null)
            .isEqualTo(outputs || outputMessages);
        String arguments = value(chatSpan, toolPrefix + SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS);
        if (outputs || outputMessages) {
            assertThat(arguments).isNull();
        } else {
            assertThat(arguments).contains("private-argument");
        }
        if (!inputs && !inputMessages) {
            assertThat(value(chatSpan, "cohere.preamble")).isEqualTo("private-preamble");
        }
        assertThat(value(chatSpan, SemanticConventions.LLM_PROVIDER)).isEqualTo("cohere");
        assertThat(value(chatSpan, SemanticConventions.LLM_REQUEST_MODEL)).isEqualTo("test-model");
        assertThat(chatSpan.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(chatSpan.getEvents()).isEmpty();
        if (inputs && outputs) {
            assertThat(chatSpan.getAttributes().toString()).doesNotContain("private-");
        }

        RerankRequest rerank = RerankRequest.builder().query("private-query")
            .documents(List.of(RerankRequestDocumentsItem.of("private-document"))).topN(1).model("rerank-model").build();
        RerankResponse ranked = RerankResponse.builder()
            .results(List.of(RerankResponseResultsItem.builder().index(0).relevanceScore(0.9f).build()))
            .build();
        client.rerankResponse = ranked;
        assertThat(traced.rerank(rerank)).isSameAs(ranked);
        assertThat(client.rerankRequest).isSameAs(rerank);
        assertThat(otel.getSpans()).hasSize(2);
        SpanData rerankSpan = otel.getSpans().get(1);
        assertThat(value(rerankSpan, SemanticConventions.GEN_AI_RERANKER_QUERY))
            .isEqualTo(inputs ? null : "private-query");
        assertThat(value(rerankSpan, SemanticConventions.RAW_INPUT) == null).isEqualTo(inputs);
        assertThat(value(rerankSpan, SemanticConventions.RAW_OUTPUT) == null).isEqualTo(outputs);
        assertThat(rerankSpan.getAttributes().get(AttributeKey.longKey(
            SemanticConventions.GEN_AI_RERANKER_INPUT_DOCUMENTS))).isEqualTo(1L);
        assertThat(rerankSpan.getAttributes().get(AttributeKey.longKey(
            SemanticConventions.GEN_AI_RERANKER_OUTPUT_DOCUMENTS))).isEqualTo(1L);
        assertThat(rerankSpan.getAttributes().get(AttributeKey.doubleKey("cohere.rerank.top_score")))
            .isEqualTo((double) 0.9f);
        assertThat(rerankSpan.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @ParameterizedTest
    @CsvSource({"chat,true", "chat,false", "embed,true", "embed,false", "rerank,true", "rerank,false"})
    void shouldApplyExplicitErrorPolicyAcrossOperations(String operation, boolean hide) {
        FakeCohere client = new FakeCohere();
        TracedCohereClient traced = traced(client, TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideErrorDetails(hide).build());
        IllegalStateException failure = new IllegalStateException("private-error");
        client.failure = failure;
        Runnable invoke;
        switch (operation) {
            case "chat":
                ChatRequest chat = ChatRequest.builder().message("private-input").build();
                invoke = () -> traced.chat(chat);
                break;
            case "embed":
                EmbedRequest embed = EmbedRequest.builder().texts(List.of("private-input")).build();
                invoke = () -> traced.embed(embed);
                break;
            default:
                RerankRequest rerank = RerankRequest.builder().query("private-input")
                    .documents(List.of(RerankRequestDocumentsItem.of("private-document"))).build();
                invoke = () -> traced.rerank(rerank);
        }
        assertThatThrownBy(invoke::run).isInstanceOf(RuntimeException.class).hasCause(failure);
        assertThat(otel.getSpans()).hasSize(1);
        SpanData span = otel.getSpans().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(value(span, SemanticConventions.ERROR_TYPE)).isEqualTo(failure.getClass().getName());
        assertThat(span.getEvents()).hasSize(1);
        if (hide) {
            assertThat(span.getStatus().getDescription()).isEmpty();
            assertThat(value(span, SemanticConventions.ERROR_MESSAGE)).isNull();
            assertThat(span.getAttributes().toString()).doesNotContain("private-");
            assertThat(span.getEvents().get(0).getAttributes().asMap())
                .containsOnlyKeys(AttributeKey.stringKey("exception.type"));
        } else {
            assertThat(span.getStatus().getDescription()).isEqualTo("private-error");
            assertThat(value(span, SemanticConventions.ERROR_MESSAGE)).isEqualTo("private-error");
            assertThat(span.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.message")))
                .isEqualTo("private-error");
        }
    }

    // Keep provider calls local and deterministic without bytecode instrumentation.
    private static class FakeCohere extends Cohere {
        ChatRequest chatRequest;
        RerankRequest rerankRequest;
        NonStreamedChatResponse chatResponse;
        RerankResponse rerankResponse;
        RuntimeException failure;

        FakeCohere() { super(ClientOptions.builder().build()); }

        @Override
        public NonStreamedChatResponse chat(ChatRequest request) {
            chatRequest = request;
            if (failure != null) throw failure;
            return chatResponse;
        }

        @Override
        public RerankResponse rerank(RerankRequest request) {
            rerankRequest = request;
            if (failure != null) throw failure;
            return rerankResponse;
        }

        @Override
        public EmbedResponse embed(EmbedRequest request) {
            throw failure;
        }
    }

    private TracedCohereClient traced(Cohere client, TraceConfig config) {
        return new TracedCohereClient(client,
            new FITracer(otel.getOpenTelemetry().getTracer("privacy-test"), config));
    }

    private String value(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }
}
