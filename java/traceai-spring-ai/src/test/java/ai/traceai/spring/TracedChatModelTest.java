package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.metadata.ChatResponseMetadata;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;

import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class TracedChatModelTest {

    private static final AttributeKey<Long> PROMPT_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
    private static final AttributeKey<Long> COMPLETION_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
    private static final AttributeKey<Long> TOTAL_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL);
    private static final AttributeKey<String> INPUT_VALUE = AttributeKey.stringKey(SemanticConventions.INPUT_VALUE);
    private static final AttributeKey<String> OUTPUT_VALUE = AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE);
    private static final AttributeKey<String> INPUT_MESSAGES = AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES);
    private static final AttributeKey<String> OUTPUT_MESSAGES = AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES);

    @Test
    void callRecordsRealInputsOutputsAndUsageWithExactIdentity() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Prompt prompt = new Prompt(List.of(new SystemMessage("be terse"), new UserMessage("hello 1.1.x")),
                ChatOptions.builder().model("synthetic-model").temperature(0.2).topP(0.9).maxTokens(64).build());
            ChatResponse expected = SpanTestSupport.response("world", SpanTestSupport.usage(11, 7, 18), "synthetic-model-v2");
            AtomicReference<Prompt> seen = new AtomicReference<>();
            ChatModel delegate = SpanTestSupport.syncModel(p -> {
                seen.set(p);
                return expected;
            });

            ChatResponse actual = new TracedChatModel(delegate, support.tracer, "synthetic").call(prompt);

            assertThat(actual).isSameAs(expected);
            assertThat(seen.get()).isSameAs(prompt);
            SpanData span = support.single();
            assertThat(span.getName()).isEqualTo("Spring AI Chat");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND))).isEqualTo("LLM");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER))).isEqualTo("synthetic");
            assertThat(span.getAttributes().get(INPUT_VALUE)).isEqualTo("be terse\nhello 1.1.x");
            assertThat(span.getAttributes().get(INPUT_MESSAGES))
                .isEqualTo("[{\"role\":\"system\",\"content\":\"be terse\"},{\"role\":\"user\",\"content\":\"hello 1.1.x\"}]");
            assertThat(span.getAttributes().get(OUTPUT_VALUE)).isEqualTo("world");
            assertThat(span.getAttributes().get(OUTPUT_MESSAGES)).isEqualTo("[{\"role\":\"assistant\",\"content\":\"world\"}]");
            assertThat(span.getAttributes().get(PROMPT_TOKENS)).isEqualTo(11L);
            assertThat(span.getAttributes().get(COMPLETION_TOKENS)).isEqualTo(7L);
            assertThat(span.getAttributes().get(TOTAL_TOKENS)).isEqualTo(18L);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL))).isEqualTo("synthetic-model");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_MODEL))).isEqualTo("synthetic-model-v2");
            assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TEMPERATURE))).isEqualTo(0.2);
            assertThat(span.getAttributes().get(AttributeKey.doubleKey(SemanticConventions.LLM_REQUEST_TOP_P))).isEqualTo(0.9);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_REQUEST_MAX_TOKENS))).isEqualTo(64L);
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void responseModelBackfillsRequestModelOnlyWhenOptionsDoNotName() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            ChatModel delegate = SpanTestSupport.syncModel(p -> SpanTestSupport.response("x", null, "from-response"));
            new TracedChatModel(delegate, support.tracer, "synthetic").call(new Prompt("no options"));

            SpanData span = support.single();
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL))).isEqualTo("from-response");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_MODEL))).isEqualTo("from-response");
        }
    }

    @Test
    void nullResponseResultMetadataAndUsageAreTolerated() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            TracedChatModel nullResponse = new TracedChatModel(SpanTestSupport.syncModel(p -> null), support.tracer, "synthetic");
            assertThat(nullResponse.call(new Prompt("a"))).isNull();

            ChatResponse emptyGenerations = new ChatResponse(List.of());
            TracedChatModel noResult = new TracedChatModel(SpanTestSupport.syncModel(p -> emptyGenerations), support.tracer, "synthetic");
            assertThat(noResult.call(new Prompt("b"))).isSameAs(emptyGenerations);

            ChatResponse nullOutput = new ChatResponse(List.of(new Generation(null)),
                ChatResponseMetadata.builder().usage(null).build());
            TracedChatModel noOutput = new TracedChatModel(SpanTestSupport.syncModel(p -> nullOutput), support.tracer, "synthetic");
            assertThat(noOutput.call(new Prompt("c"))).isSameAs(nullOutput);

            List<SpanData> spans = support.finished();
            assertThat(spans).hasSize(3);
            for (SpanData span : spans) {
                assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
                assertThat(span.getAttributes().get(OUTPUT_VALUE)).isNull();
                assertThat(span.getAttributes().get(PROMPT_TOKENS)).isNull();
                assertThat(span.getAttributes().get(COMPLETION_TOKENS)).isNull();
                assertThat(span.getAttributes().get(TOTAL_TOKENS)).isNull();
            }
            assertThat(support.spansEnded.get()).isEqualTo(3);
        }
    }

    @Test
    void tokenCountsAreIndependentlyNullableAndZeroIsPreserved() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            ChatResponse zeroCompletion = SpanTestSupport.response("z", SpanTestSupport.usage(5, 0, 5), null);
            ChatResponse onlyPrompt = SpanTestSupport.response("p", SpanTestSupport.usage(9, null, null, null), null);
            ChatResponse onlyTotal = SpanTestSupport.response("t", SpanTestSupport.usage(null, null, 42, null), null);
            TracedChatModel model = new TracedChatModel(SpanTestSupport.syncModel(p -> {
                String text = p.getContents();
                return text.equals("zero") ? zeroCompletion : text.equals("prompt") ? onlyPrompt : onlyTotal;
            }), support.tracer, "synthetic");

            model.call(new Prompt("zero"));
            model.call(new Prompt("prompt"));
            model.call(new Prompt("total"));

            List<SpanData> spans = support.finished();
            assertThat(spans.get(0).getAttributes().get(PROMPT_TOKENS)).isEqualTo(5L);
            assertThat(spans.get(0).getAttributes().get(COMPLETION_TOKENS)).isEqualTo(0L);
            assertThat(spans.get(0).getAttributes().get(TOTAL_TOKENS)).isEqualTo(5L);

            assertThat(spans.get(1).getAttributes().get(PROMPT_TOKENS)).isEqualTo(9L);
            assertThat(spans.get(1).getAttributes().get(COMPLETION_TOKENS)).isNull();
            assertThat(spans.get(1).getAttributes().get(TOTAL_TOKENS)).isNull();

            assertThat(spans.get(2).getAttributes().get(PROMPT_TOKENS)).isNull();
            assertThat(spans.get(2).getAttributes().get(COMPLETION_TOKENS)).isNull();
            assertThat(spans.get(2).getAttributes().get(TOTAL_TOKENS)).isEqualTo(42L);
        }
    }

    @Test
    void runtimeExceptionKeepsIdentityAndEndsSpanWithError() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            IllegalStateException failure = new IllegalStateException("synthetic provider failure");
            TracedChatModel model = new TracedChatModel(SpanTestSupport.syncModel(p -> {
                throw failure;
            }), support.tracer, "synthetic");

            assertThatThrownBy(() -> model.call(new Prompt("boom"))).isSameAs(failure);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)))
                .isEqualTo(IllegalStateException.class.getName());
            assertThat(span.getEvents()).anySatisfy(event -> assertThat(event.getName()).isEqualTo("exception"));
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void syntheticLinkageErrorEscapesUnchangedAfterSpanCleanup() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            NoSuchMethodError failure = new NoSuchMethodError("synthetic Message.getContent()");
            TracedChatModel model = new TracedChatModel(SpanTestSupport.syncModel(p -> {
                throw failure;
            }), support.tracer, "synthetic");

            assertThatThrownBy(() -> model.call(new Prompt("link"))).isSameAs(failure);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isNotEqualTo(StatusCode.OK);
            assertThat(span.hasEnded()).isTrue();
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void defaultOptionsAndUnwrapDelegateExactly() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            ChatOptions defaults = ChatOptions.builder().model("d").build();
            ChatModel delegate = new ChatModel() {
                @Override
                public ChatResponse call(Prompt prompt) {
                    return null;
                }

                @Override
                public ChatOptions getDefaultOptions() {
                    return defaults;
                }
            };
            TracedChatModel model = new TracedChatModel(delegate, support.tracer, "synthetic");
            assertThat(model.getDefaultOptions()).isSameAs(defaults);
            assertThat(model.unwrap()).isSameAs(delegate);
            assertThat(support.spansStarted.get()).isZero();
        }
    }
}
