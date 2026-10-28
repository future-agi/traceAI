package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.publisher.Flux;
import reactor.test.StepVerifier;

import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;

/** The four privacy switches are independent; both must be set to hide a channel completely. */
class TracingPrivacyTest {

    private static final Duration TIMEOUT = Duration.ofSeconds(5);
    private static final AttributeKey<String> INPUT_VALUE = AttributeKey.stringKey(SemanticConventions.INPUT_VALUE);
    private static final AttributeKey<String> INPUT_MESSAGES = AttributeKey.stringKey(SemanticConventions.LLM_INPUT_MESSAGES);
    private static final AttributeKey<String> OUTPUT_VALUE = AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE);
    private static final AttributeKey<String> OUTPUT_MESSAGES = AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES);

    private static SpanData traced(TraceConfig config, Flux<ChatResponse> upstream) {
        try (SpanTestSupport support = new SpanTestSupport(config)) {
            StepVerifier.create(new TracedChatModel(SpanTestSupport.streamModel(() -> upstream), support.tracer, "synthetic")
                    .stream(new Prompt("secret input")))
                .expectNextCount(1)
                .expectComplete()
                .verify(TIMEOUT);
            return support.single();
        }
    }

    @Test
    void hideInputsAloneKeepsMessages() {
        SpanData span = traced(TraceConfig.builder().hideInputs(true).build(), Flux.just(SpanTestSupport.response("visible")));
        assertThat(span.getAttributes().get(INPUT_VALUE)).isNull();
        assertThat(span.getAttributes().get(INPUT_MESSAGES)).contains("secret input");
        assertThat(span.getAttributes().get(OUTPUT_VALUE)).isEqualTo("visible");
    }

    @Test
    void hideInputMessagesAloneKeepsInputValue() {
        SpanData span = traced(TraceConfig.builder().hideInputMessages(true).build(), Flux.just(SpanTestSupport.response("visible")));
        assertThat(span.getAttributes().get(INPUT_VALUE)).isEqualTo("secret input");
        assertThat(span.getAttributes().get(INPUT_MESSAGES)).isNull();
    }

    @Test
    void hideOutputsAloneKeepsOutputMessages() {
        SpanData span = traced(TraceConfig.builder().hideOutputs(true).build(), Flux.just(SpanTestSupport.response("visible")));
        assertThat(span.getAttributes().get(OUTPUT_VALUE)).isNull();
        assertThat(span.getAttributes().get(OUTPUT_MESSAGES)).contains("visible");
    }

    @Test
    void bothOutputSwitchesAllocateNoBufferAndHideEverything() {
        SpanData span = traced(TraceConfig.builder().hideOutputs(true).hideOutputMessages(true).build(),
            Flux.just(SpanTestSupport.response("should-not-be-captured")));
        assertThat(span.getAttributes().get(OUTPUT_VALUE)).isNull();
        assertThat(span.getAttributes().get(OUTPUT_MESSAGES)).isNull();
    }

    @Test
    void allFourSwitchesTogetherHideBothChannels() {
        SpanData span = traced(TraceConfig.builder().hideInputs(true).hideInputMessages(true)
                .hideOutputs(true).hideOutputMessages(true).build(),
            Flux.just(SpanTestSupport.response("hidden")));
        assertThat(span.getAttributes().get(INPUT_VALUE)).isNull();
        assertThat(span.getAttributes().get(INPUT_MESSAGES)).isNull();
        assertThat(span.getAttributes().get(OUTPUT_VALUE)).isNull();
        assertThat(span.getAttributes().get(OUTPUT_MESSAGES)).isNull();
    }

    @Test
    void allowedMetadataSurvivesEveryPrivacySwitch() {
        try (SpanTestSupport support = new SpanTestSupport(TraceConfig.builder().hideInputs(true).hideInputMessages(true)
                .hideOutputs(true).hideOutputMessages(true).build())) {
            StepVerifier.create(new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(SpanTestSupport.response("x"))),
                    support.tracer, "synthetic")
                    .stream(new Prompt("hi"))
                    .contextWrite(SpringAITracingContext.withContext(io.opentelemetry.context.Context.root(),
                        java.util.Map.of(SemanticConventions.USER_ID, "kept-user"))))
                .expectNextCount(1).expectComplete().verify(TIMEOUT);
            assertThat(support.single().getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isEqualTo("kept-user");
        }
    }

    @Test
    void streamOutputIsTruncatedAtTheAttributeBudgetWithoutChangingDeliveredChunks() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            String huge = "x".repeat(40_000);
            ChatResponse chunk = SpanTestSupport.response(huge);
            StepVerifier.create(new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(chunk)), support.tracer, "synthetic")
                    .stream(new Prompt("hi")))
                .expectNextMatches(delivered -> delivered == chunk && delivered.getResult().getOutput().getText().length() == 40_000)
                .expectComplete()
                .verify(TIMEOUT);
            String captured = support.single().getAttributes().get(OUTPUT_VALUE);
            assertThat(captured).hasSize(32_000).endsWith("...[truncated]");
        }
    }
}
