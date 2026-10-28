package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.metadata.ChatResponseMetadata;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.publisher.Flux;
import reactor.test.StepVerifier;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.assertThat;

class TracedChatModelStreamTest {

    private static final Duration TIMEOUT = Duration.ofSeconds(5);
    private static final AttributeKey<String> OUTPUT_VALUE = AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE);
    private static final AttributeKey<String> OUTPUT_MESSAGES = AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES);
    private static final AttributeKey<Long> PROMPT_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
    private static final AttributeKey<Long> COMPLETION_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION);
    private static final AttributeKey<Long> TOTAL_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL);

    @Test
    void streamAssemblyDoesNotStartSpanOrInvokeDelegate() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            AtomicInteger delegateCalls = new AtomicInteger();
            ChatModel delegate = SpanTestSupport.streamModel(() -> {
                delegateCalls.incrementAndGet();
                return Flux.empty();
            });
            TracedChatModel model = new TracedChatModel(delegate, support.tracer, "synthetic");

            Flux<ChatResponse> stream = model.stream(new Prompt("synthetic assembly input"));

            assertThat(stream).isNotNull();
            assertThat(support.spansStarted.get()).as("stream(prompt) must not start a span before subscription").isZero();
            assertThat(delegateCalls.get()).as("stream(prompt) must not call the delegate before subscription").isZero();
            assertThat(support.finished()).isEmpty();
        }
    }

    @Test
    void threeChunksAreDeliveredIdenticallyAndOutputIsConcatenated() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            ChatResponse c1 = SpanTestSupport.response("Hel");
            ChatResponse c2 = SpanTestSupport.response("lo ");
            ChatResponse c3 = SpanTestSupport.response("world");
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(c1, c2, c3)), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("hi")))
                .expectNextMatches(r -> r == c1)
                .expectNextMatches(r -> r == c2)
                .expectNextMatches(r -> r == c3)
                .expectComplete()
                .verify(TIMEOUT);

            SpanData span = support.single();
            assertThat(span.getName()).isEqualTo("Spring AI Chat (Stream)");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(OUTPUT_VALUE)).isEqualTo("Hello world");
            assertThat(span.getAttributes().get(OUTPUT_MESSAGES)).isEqualTo("[{\"role\":\"assistant\",\"content\":\"Hello world\"}]");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.INPUT_VALUE))).isEqualTo("hi");
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void usageOnlyChunkIsForwardedWithoutTokenAggregation() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            ChatResponse text = SpanTestSupport.response("done");
            ChatResponse usageOnly = new ChatResponse(List.of(new Generation(new AssistantMessage(""))),
                ChatResponseMetadata.builder().usage(SpanTestSupport.usage(3, 4, 7)).build());
            ChatResponse metadataOnly = new ChatResponse(List.of(), ChatResponseMetadata.builder().model("m").build());
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(text, usageOnly, metadataOnly)),
                support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("hi")))
                .expectNextMatches(r -> r == text)
                .expectNextMatches(r -> r == usageOnly)
                .expectNextMatches(r -> r == metadataOnly)
                .expectComplete()
                .verify(TIMEOUT);

            SpanData span = support.single();
            assertThat(span.getAttributes().get(OUTPUT_VALUE)).isEqualTo("done");
            assertThat(span.getAttributes().get(PROMPT_TOKENS)).isNull();
            assertThat(span.getAttributes().get(COMPLETION_TOKENS)).isNull();
            assertThat(span.getAttributes().get(TOTAL_TOKENS)).isNull();
        }
    }

    @Test
    void emptyStreamCompletesWithOneOkSpan() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(Flux::<ChatResponse>empty), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("hi"))).expectComplete().verify(TIMEOUT);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(OUTPUT_VALUE)).isEqualTo("");
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void repeatedSubscriptionsOwnSeparateSpans() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            AtomicInteger delegateCalls = new AtomicInteger();
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> {
                delegateCalls.incrementAndGet();
                return Flux.just(SpanTestSupport.response("a"));
            }), support.tracer, "synthetic");
            Flux<ChatResponse> stream = model.stream(new Prompt("reuse"));

            StepVerifier.create(stream).expectNextCount(1).expectComplete().verify(TIMEOUT);
            StepVerifier.create(stream).expectNextCount(1).expectComplete().verify(TIMEOUT);

            assertThat(delegateCalls.get()).isEqualTo(2);
            List<SpanData> spans = support.finished();
            assertThat(spans).hasSize(2);
            assertThat(spans.get(0).getSpanContext().getSpanId()).isNotEqualTo(spans.get(1).getSpanContext().getSpanId());
            assertThat(support.spansEnded.get()).isEqualTo(2);
        }
    }

    @Test
    void concurrentSubscriptionsEachEndExactlyOnce() throws InterruptedException {
        try (SpanTestSupport support = new SpanTestSupport()) {
            CountDownLatch release = new CountDownLatch(1);
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() ->
                Flux.defer(() -> {
                    try {
                        release.await(5, TimeUnit.SECONDS);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                    return Flux.just(SpanTestSupport.response("x"), SpanTestSupport.response("y"));
                })), support.tracer, "synthetic");
            Flux<ChatResponse> stream = model.stream(new Prompt("parallel"));

            CountDownLatch done = new CountDownLatch(2);
            Runnable subscriber = () -> stream.subscribe(r -> { }, e -> done.countDown(), done::countDown);
            Thread t1 = new Thread(subscriber);
            Thread t2 = new Thread(subscriber);
            t1.start();
            t2.start();
            release.countDown();
            assertThat(done.await(5, TimeUnit.SECONDS)).isTrue();
            t1.join(5_000);
            t2.join(5_000);

            assertThat(support.finished()).hasSize(2);
            assertThat(support.spansStarted.get()).isEqualTo(2);
            assertThat(support.spansEnded.get()).isEqualTo(2);
            assertThat(support.finished()).allSatisfy(s -> {
                assertThat(s.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
                assertThat(s.getAttributes().get(OUTPUT_VALUE)).isEqualTo("xy");
            });
        }
    }

    @Test
    void demandIsForwardedExactlyWithoutEagerRequests() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            AtomicLong requested = new AtomicLong();
            Flux<ChatResponse> upstream = Flux.just(SpanTestSupport.response("1"), SpanTestSupport.response("2"), SpanTestSupport.response("3"))
                .doOnRequest(requested::addAndGet);
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> upstream), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("demand")), 0)
                .expectSubscription()
                .then(() -> assertThat(requested.get()).isZero())
                .thenRequest(1)
                .expectNextCount(1)
                .then(() -> assertThat(requested.get()).isEqualTo(1))
                .thenRequest(2)
                .expectNextCount(2)
                .expectComplete()
                .verify(TIMEOUT);

            assertThat(requested.get()).isEqualTo(3);
            assertThat(support.single().getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        }
    }
}
