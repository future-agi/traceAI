package ai.traceai.spring;

import ai.traceai.ContextAttributes;
import ai.traceai.SemanticConventions;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import reactor.core.publisher.Flux;
import reactor.core.scheduler.Schedulers;
import reactor.test.StepVerifier;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class SpringAITracingContextTest {

    private static final class QuietScope implements AutoCloseable {
        private final AutoCloseable delegate;
        QuietScope(AutoCloseable delegate) { this.delegate = delegate; }
        @Override public void close() {
            try { delegate.close(); } catch (RuntimeException e) { throw e; } catch (Exception e) { throw new IllegalStateException(e); }
        }
    }


    private static final Duration TIMEOUT = Duration.ofSeconds(5);

    private static SpanData streamSpan(SpanTestSupport support, Flux<ChatResponse> stream) {
        StepVerifier.create(stream).expectNextCount(1).expectComplete().verify(TIMEOUT);
        return support.single();
    }

    @Test
    void explicitRootAndEmptyMapOverridePopulatedAssemblySnapshot() {
        try (SpanTestSupport support = new SpanTestSupport();
             var session = new QuietScope(ContextAttributes.usingSession("assembly-session"));
             var user = new QuietScope(ContextAttributes.usingUser("assembly-user"))) {
            Flux<ChatResponse> stream = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(SpanTestSupport.response("x"))),
                support.tracer, "synthetic")
                .stream(new Prompt("hi"))
                .contextWrite(SpringAITracingContext.withContext(Context.root(), Map.of()));

            SpanData span = streamSpan(support, stream);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.SESSION_ID))).isNull();
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isNull();
            assertThat(span.getParentSpanContext().isValid()).isFalse();
        }
    }

    @Test
    void serializedMetadataAndTagsAreAppliedVerbatim() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Map<String, String> attributes = new LinkedHashMap<>();
            attributes.put(SemanticConventions.METADATA, "{\"n\":7,\"flag\":true}");
            attributes.put(SemanticConventions.TAG_TAGS, "[\"a\",\"b\"]");
            attributes.put(SemanticConventions.SESSION_ID, "s-1");
            attributes.put(SemanticConventions.GEN_AI_CONVERSATION_ID, "s-1");
            attributes.put(SemanticConventions.USER_ID, "u-1");

            Flux<ChatResponse> stream = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(SpanTestSupport.response("x"))),
                support.tracer, "synthetic")
                .stream(new Prompt("hi"))
                .contextWrite(SpringAITracingContext.withContext(Context.root(), attributes));

            SpanData span = streamSpan(support, stream);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.METADATA))).isEqualTo("{\"n\":7,\"flag\":true}");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.TAG_TAGS))).isEqualTo("[\"a\",\"b\"]");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.SESSION_ID))).isEqualTo("s-1");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isEqualTo("u-1");
        }
    }

    @Test
    void unknownKeysAndNullValuesAreRejectedBeforeSubscription() {
        assertThatThrownBy(() -> SpringAITracingContext.withContext(Context.root(), Map.of("not.a.key", "v")))
            .isInstanceOf(IllegalArgumentException.class);
        Map<String, String> nullValue = new LinkedHashMap<>();
        nullValue.put(SemanticConventions.USER_ID, null);
        assertThatThrownBy(() -> SpringAITracingContext.withContext(Context.root(), nullValue))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> SpringAITracingContext.withContext(null, Map.of()))
            .isInstanceOf(NullPointerException.class);
    }

    @Test
    void staleWorkerThreadAttributesDoNotLeakOntoTheSpanAndAreRestored() throws InterruptedException {
        try (SpanTestSupport support = new SpanTestSupport()) {
            AtomicReference<String> seenDuringDelegate = new AtomicReference<>("unset");
            CountDownLatch done = new CountDownLatch(1);
            Schedulers.boundedElastic().schedule(() -> {
                try (var ignored = new QuietScope(ContextAttributes.usingUser("stale-worker-user"))) {
                    Flux<ChatResponse> stream = new TracedChatModel(SpanTestSupport.streamModel(() -> {
                        seenDuringDelegate.set(String.valueOf(ContextAttributes.getAttributesFromContext().get(SemanticConventions.USER_ID)));
                        return Flux.just(SpanTestSupport.response("x"));
                    }), support.tracer, "synthetic")
                        .stream(new Prompt("hi"))
                        .contextWrite(SpringAITracingContext.withContext(Context.root(),
                            Map.of(SemanticConventions.USER_ID, "explicit-user")));
                    StepVerifier.create(stream).expectNextCount(1).expectComplete().verify(TIMEOUT);
                    assertThat(ContextAttributes.getAttributesFromContext().get(SemanticConventions.USER_ID))
                        .as("worker thread attributes restored after the span").isEqualTo("stale-worker-user");
                } finally {
                    done.countDown();
                }
            });
            assertThat(done.await(10, TimeUnit.SECONDS)).isTrue();

            SpanData span = support.single();
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isEqualTo("explicit-user");
            assertThat(seenDuringDelegate.get()).isEqualTo("stale-worker-user");
        }
    }

    @Test
    void schedulerHopKeepsTheExplicitSnapshot() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Flux<ChatResponse> stream = new TracedChatModel(SpanTestSupport.streamModel(() ->
                Flux.just(SpanTestSupport.response("x")).subscribeOn(Schedulers.boundedElastic())), support.tracer, "synthetic")
                .stream(new Prompt("hi"))
                .contextWrite(SpringAITracingContext.withContext(Context.root(),
                    Map.of(SemanticConventions.SESSION_ID, "hop-session")));

            SpanData span = streamSpan(support, stream);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.SESSION_ID))).isEqualTo("hop-session");
        }
    }

    @Test
    void twoSyntheticTenantsStayIsolatedOnOnePublisher() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Flux<ChatResponse> shared = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(SpanTestSupport.response("x"))),
                support.tracer, "synthetic").stream(new Prompt("hi"));

            StepVerifier.create(shared.contextWrite(SpringAITracingContext.withContext(Context.root(),
                Map.of(SemanticConventions.USER_ID, "tenant-a")))).expectNextCount(1).expectComplete().verify(TIMEOUT);
            StepVerifier.create(shared.contextWrite(SpringAITracingContext.withContext(Context.root(),
                Map.of(SemanticConventions.USER_ID, "tenant-b")))).expectNextCount(1).expectComplete().verify(TIMEOUT);

            List<SpanData> spans = support.finished();
            assertThat(spans).hasSize(2);
            assertThat(spans.get(0).getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isEqualTo("tenant-a");
            assertThat(spans.get(1).getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isEqualTo("tenant-b");
        }
    }

    @Test
    void subscriptionTimeAttributesWinWhenNoExplicitSnapshotExists() {
        try (SpanTestSupport support = new SpanTestSupport();
             var user = new QuietScope(ContextAttributes.usingUser("subscription-user"))) {
            Flux<ChatResponse> stream = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(SpanTestSupport.response("x"))),
                support.tracer, "synthetic").stream(new Prompt("hi"));
            SpanData span = streamSpan(support, stream);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.USER_ID))).isEqualTo("subscription-user");
        }
    }

    @Test
    void explicitParentSpanIsTheSpanParent() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Span parent = support.tracer.startSpan("parent", ai.traceai.FISpanKind.AGENT);
            try {
                Flux<ChatResponse> stream = new TracedChatModel(SpanTestSupport.streamModel(() -> Flux.just(SpanTestSupport.response("x"))),
                    support.tracer, "synthetic")
                    .stream(new Prompt("hi"))
                    .contextWrite(SpringAITracingContext.withContext(Context.current().with(parent), Map.of()));
                SpanData span = streamSpan(support, stream);
                assertThat(span.getParentSpanContext().getSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            } finally {
                parent.end();
            }
        }
    }
}
