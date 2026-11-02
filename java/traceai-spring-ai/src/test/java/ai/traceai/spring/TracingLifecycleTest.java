package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.reactivestreams.Subscription;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.CoreSubscriber;
import reactor.core.publisher.BaseSubscriber;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;
import reactor.test.StepVerifier;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Adversarial lifecycle suite: exactly one span end and one export per subscription, whatever terminates it. */
class TracingLifecycleTest {

    private static final Duration TIMEOUT = Duration.ofSeconds(5);
    private static final AttributeKey<String> OUTPUT_VALUE = AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE);

    @Test
    void cancelAfterFirstChunkEndsSpanOnceAsCancelled() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Sinks.Many<ChatResponse> sink = Sinks.many().unicast().onBackpressureBuffer();
            AtomicInteger upstreamCancels = new AtomicInteger();
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() ->
                sink.asFlux().doOnCancel(upstreamCancels::incrementAndGet)), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("cancel")))
                .then(() -> sink.tryEmitNext(SpanTestSupport.response("partial")))
                .expectNextCount(1)
                .thenCancel()
                .verify(TIMEOUT);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEqualTo("cancelled");
            assertThat(span.getAttributes().get(OUTPUT_VALUE)).as("partial output is not recorded as success").isNull();
            assertThat(upstreamCancels.get()).isEqualTo(1);
            assertThat(support.spansEnded.get()).isEqualTo(1);

            // late signals after cancel must not reopen or re-end the span
            sink.tryEmitNext(SpanTestSupport.response("late"));
            sink.tryEmitComplete();
            assertThat(support.spansEnded.get()).isEqualTo(1);
            assertThat(support.finished()).hasSize(1);
        }
    }

    @Test
    void immediateCancelFromOnSubscribePreventsDeliveryAndClosesSpan() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            AtomicInteger delivered = new AtomicInteger();
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() ->
                Flux.just(SpanTestSupport.response("never"))), support.tracer, "synthetic");

            model.stream(new Prompt("immediate")).subscribe(new BaseSubscriber<>() {
                @Override
                protected void hookOnSubscribe(Subscription subscription) {
                    subscription.cancel();
                }

                @Override
                protected void hookOnNext(ChatResponse value) {
                    delivered.incrementAndGet();
                }
            });

            assertThat(delivered.get()).isZero();
            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEqualTo("cancelled");
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void delegateStreamThrowingBeforeReturnSignalsErrorAndEndsSpanOnce() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            IllegalStateException failure = new IllegalStateException("synthetic pre-return failure");
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> {
                throw failure;
            }), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("pre-return")))
                .expectErrorSatisfies(e -> assertThat(e).isSameAs(failure))
                .verify(TIMEOUT);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)))
                .isEqualTo(IllegalStateException.class.getName());
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void subscribeThrowingAfterReturnSignalsErrorAndEndsSpanOnce() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            IllegalArgumentException failure = new IllegalArgumentException("synthetic subscribe failure");
            Flux<ChatResponse> hostile = new Flux<>() {
                @Override
                public void subscribe(CoreSubscriber<? super ChatResponse> actual) {
                    throw failure;
                }
            };
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> hostile), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("subscribe")))
                .expectErrorSatisfies(e -> assertThat(e).isSameAs(failure))
                .verify(TIMEOUT);

            assertThat(support.single().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void requestThrowingIsRecordedOnceAndForwardedAsError() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            IllegalStateException failure = new IllegalStateException("synthetic request failure");
            Flux<ChatResponse> hostile = new Flux<>() {
                @Override
                public void subscribe(CoreSubscriber<? super ChatResponse> actual) {
                    actual.onSubscribe(new Subscription() {
                        @Override
                        public void request(long n) {
                            throw failure;
                        }

                        @Override
                        public void cancel() {
                        }
                    });
                }
            };
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> hostile), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("request")))
                .expectErrorSatisfies(e -> assertThat(e).isSameAs(failure))
                .verify(TIMEOUT);

            assertThat(support.single().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void upstreamErrorKeepsIdentityAndDoesNotRecordPartialOutputAsSuccess() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            RuntimeException failure = new RuntimeException("synthetic mid-stream failure");
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() ->
                Flux.concat(Flux.just(SpanTestSupport.response("partial")), Flux.error(failure))), support.tracer, "synthetic");

            StepVerifier.create(model.stream(new Prompt("error")))
                .expectNextCount(1)
                .expectErrorSatisfies(e -> assertThat(e).isSameAs(failure))
                .verify(TIMEOUT);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().get(OUTPUT_VALUE)).isNull();
            assertThat(span.getEvents()).anySatisfy(event -> assertThat(event.getName()).isEqualTo("exception"));
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void syntheticLinkageErrorFromDelegateStreamPropagatesAfterCleanup() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            NoSuchMethodError failure = new NoSuchMethodError("synthetic linkage");
            TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() -> {
                throw failure;
            }), support.tracer, "synthetic");

            Flux<ChatResponse> stream = model.stream(new Prompt("linkage"));
            assertThatThrownBy(() -> stream.subscribe()).isSameAs(failure);

            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isNotEqualTo(StatusCode.OK);
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }

    @Test
    void concurrentCompleteAndCancelEndTheSpanExactlyOnce() throws InterruptedException {
        int decided = 0;
        for (int round = 0; round < 60 && decided < 8; round++) {
            try (SpanTestSupport support = new SpanTestSupport()) {
                CountDownLatch release = new CountDownLatch(1);
                TracedChatModel model = new TracedChatModel(SpanTestSupport.streamModel(() ->
                    Flux.just(SpanTestSupport.response("x")).delaySubscription(Duration.ofMillis(5))
                        .doOnSubscribe(s -> release.countDown())), support.tracer, "synthetic");
                AtomicReference<Subscription> subscription = new AtomicReference<>();
                CountDownLatch downstreamSubscribed = new CountDownLatch(1);
                model.stream(new Prompt("race")).subscribe(new BaseSubscriber<>() {
                    @Override
                    protected void hookOnSubscribe(Subscription s) {
                        subscription.set(s);
                        downstreamSubscribed.countDown();
                        s.request(Long.MAX_VALUE);
                    }
                });
                assertThat(downstreamSubscribed.await(5, TimeUnit.SECONDS)).isTrue();
                release.await(5, TimeUnit.SECONDS);

                CountDownLatch go = new CountDownLatch(1);
                Thread canceller = new Thread(() -> {
                    await(go);
                    subscription.get().cancel();
                });
                canceller.start();
                go.countDown();
                canceller.join(5_000);

                // whichever terminal wins, exactly one span is exported and it has ended
                long deadline = System.currentTimeMillis() + 5_000;
                while (support.finished().isEmpty() && System.currentTimeMillis() < deadline) {
                    Thread.sleep(10);
                }
                List<SpanData> spans = support.finished();
                decided++;
                assertThat(spans).as("round " + round).hasSize(1);
                assertThat(support.spansEnded.get()).as("round " + round).isEqualTo(1);
                assertThat(spans.get(0).hasEnded()).isTrue();
            }
        }
        assertThat(decided).isGreaterThanOrEqualTo(8);
    }

    private static void await(CountDownLatch latch) {
        try {
            latch.await(5, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
