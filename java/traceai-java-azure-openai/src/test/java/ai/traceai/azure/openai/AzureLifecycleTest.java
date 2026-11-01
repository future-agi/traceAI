package ai.traceai.azure.openai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.azure.ai.openai.OpenAIAsyncClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.ai.openai.models.*;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.*;
import com.azure.core.http.policy.FixedDelayOptions;
import com.azure.core.http.policy.RetryOptions;
import com.azure.core.util.IterableStream;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import org.junit.jupiter.api.Timeout;
import org.reactivestreams.Subscription;
import reactor.core.publisher.BaseSubscriber;
import reactor.core.scheduler.Schedulers;
import java.util.ArrayList;
import java.util.concurrent.atomic.AtomicBoolean;

/** Acceptance tests use the released Azure SDK and synthetic, in-process HTTP only. */
class AzureLifecycleTest {
    private static final String CHAT = """
        {"id":"chat-test","created":1700000000,"model":"model-version",
         "choices":[{"index":0,"message":{"role":"assistant","content":"Paris"},"finish_reason":"stop"}],
         "usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}
        """;
    private static final String EVENT = """
        {"id":"chat-stream","created":1700000000,"model":"model-version",
         "choices":[{"index":0,"delta":{"role":"assistant","content":"Paris"},"finish_reason":"stop"}]}
        """.replace("\n", "");
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider provider = SdkTracerProvider.builder()
        .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final FITracer tracer = new FITracer(provider.get("azure-lifecycle"));
    private final AtomicInteger requests = new AtomicInteger();

    @AfterEach void close() { provider.close(); }

    @Test
    @SuppressWarnings("unchecked")
    void streamingMustExposeATracedSdkCompatibleMethod() throws Exception {
        var client = new TracedAzureOpenAIClient(builder(sse(EVENT), "text/event-stream").buildClient(), tracer);
        var method = client.getClass().getMethod("getChatCompletionsStream", String.class, ChatCompletionsOptions.class);
        var result = (IterableStream<ChatCompletions>) method.invoke(client, "deployment", options());
        try (var stream = result.stream()) {
            assertThat(stream.toList().get(0).getChoices().get(0).getDelta().getContent()).isEqualTo("Paris");
        }
        assertAzureSpan();
    }

    @Test
    @SuppressWarnings("unchecked")
    void asyncMustExposeATracedSdkCompatibleMethod() throws Exception {
        var sdk = builder(Flux.just(ByteBuffer.wrap(CHAT.getBytes(StandardCharsets.UTF_8))), "application/json")
            .buildAsyncClient();
        var type = Class.forName("ai.traceai.azure.openai.TracedAzureOpenAIAsyncClient");
        var client = type.getConstructor(OpenAIAsyncClient.class, FITracer.class).newInstance(sdk, tracer);
        var method = type.getMethod("getChatCompletions", String.class, ChatCompletionsOptions.class);
        var result = (Mono<ChatCompletions>) method.invoke(client, "deployment", options());
        assertThat(requests.get()).isZero();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        assertThat(result.block(Duration.ofSeconds(5)).getChoices().get(0).getMessage().getContent()).isEqualTo("Paris");
        assertAzureSpan();
    }

    @Test
    void asyncSubscriptionsAreColdIndependentAndKeepAssemblyParentAcrossThreads() {
        var sdk = builder(bytes(CHAT), "application/json").buildAsyncClient();
        var client = new TracedAzureOpenAIAsyncClient(sdk, tracer);
        assertThat(client.unwrap()).isSameAs(sdk);
        Span parent = provider.get("parent").spanBuilder("parent").startSpan();
        Mono<ChatCompletions> result;
        try (Scope ignored = parent.makeCurrent()) {
            result = client.getChatCompletions("deployment", options());
            assertThat(Span.current()).isEqualTo(parent);
        }
        assertThat(requests.get()).isZero();
        result.subscribeOn(Schedulers.boundedElastic()).block(Duration.ofSeconds(5));
        result.subscribeOn(Schedulers.boundedElastic()).block(Duration.ofSeconds(5));
        assertThat(requests.get()).isEqualTo(2);
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
                .isEqualTo(9L);
        });
        assertThat(exporter.getFinishedSpanItems().get(0).getSpanId())
            .isNotEqualTo(exporter.getFinishedSpanItems().get(1).getSpanId());
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
        parent.end();
    }

    @Test
    @Timeout(5)
    void syncStreamYieldsBeforeCompletionAndCloseCancelsWithoutLeakingContext() {
        AtomicInteger cancelled = new AtomicInteger();
        Flux<ByteBuffer> body = Flux.concat(event(EVENT), Flux.<ByteBuffer>never())
            .doOnCancel(cancelled::incrementAndGet);
        var client = new TracedAzureOpenAIClient(builder(body, "text/event-stream").buildClient(), tracer);
        Span parent = provider.get("parent").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent();
             var result = client.getChatCompletionsStream("deployment", options())) {
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            var iterator = result.iterator();
            assertThat(iterator.next().getChoices().get(0).getDelta().getContent()).isEqualTo("Paris");
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            assertThat(Span.current()).isEqualTo(parent);
        }
        // beta.16 caches its SSE source; consumer cancellation does not reach HTTP.
        assertThat(cancelled.get()).isZero();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
        assertThat(span.getAttributes().get(AttributeKey.booleanKey("azure.openai.cancelled"))).isTrue();
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("Paris");
        parent.end();
    }

    @Test
    @Timeout(5)
    void closingJavaStreamCancelsAndEndsOnce() {
        AtomicInteger cancelled = new AtomicInteger();
        var client = new TracedAzureOpenAIClient(builder(
            Flux.concat(event(EVENT), Flux.<ByteBuffer>never()).doOnCancel(cancelled::incrementAndGet),
            "text/event-stream").buildClient(), tracer);
        var result = client.getChatCompletionsStream("deployment", options());
        try (var stream = result.stream()) {
            assertThat(stream.findFirst()).isPresent();
        }
        result.close();
        result.close();
        // beta.16 caches its SSE source; consumer cancellation does not reach HTTP.
        assertThat(cancelled.get()).isZero();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    @Timeout(5)
    void closingUnconsumedResultEndsOnceWithoutStartingSdkBody() {
        AtomicInteger cancelled = new AtomicInteger();
        AtomicInteger subscriptions = new AtomicInteger();
        var body = Flux.<ByteBuffer>never().doOnSubscribe(ignored -> subscriptions.incrementAndGet())
            .doOnCancel(cancelled::incrementAndGet);
        var client = new TracedAzureOpenAIClient(builder(body, "text/event-stream").buildClient(), tracer);
        var result = client.getChatCompletionsStream("deployment", options());
        result.close();
        result.close();
        assertThat(subscriptions.get()).isZero();
        // beta.16 caches its SSE source; consumer cancellation does not reach HTTP.
        assertThat(cancelled.get()).isZero();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void fullSyncStreamRetainsUsageAndDoesNotResubscribeOnClose() {
        AtomicInteger subscriptions = new AtomicInteger();
        String usage = "{\"id\":\"chat-stream\",\"created\":1700000000,\"model\":\"model-version\","
            + "\"choices\":[],\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":2,\"total_tokens\":9}}";
        var body = Flux.concat(event(EVENT), sse(usage)).doOnSubscribe(ignored -> subscriptions.incrementAndGet());
        var client = new TracedAzureOpenAIClient(builder(body, "text/event-stream").buildClient(), tracer);
        try (var result = client.getChatCompletionsStream("deployment", options(),
                new ChatCompletionStreamOptions().setIncludeUsage(true)); var stream = result.stream()) {
            assertThat(stream.toList()).hasSize(2);
        }
        assertThat(subscriptions.get()).isEqualTo(1);
        assertAzureSpan();
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
            .isEqualTo(9L);
    }

    @Test
    void asyncStreamHonorsDemandAndCancellation() throws Exception {
        AtomicInteger cancelled = new AtomicInteger();
        var body = Flux.concat(event(EVENT), Flux.<ByteBuffer>never()).doOnCancel(cancelled::incrementAndGet);
        var client = new TracedAzureOpenAIAsyncClient(builder(body, "text/event-stream").buildAsyncClient(), tracer);
        List<ChatCompletions> received = new java.util.concurrent.CopyOnWriteArrayList<>();
        var first = new java.util.concurrent.CountDownLatch(1);
        BaseSubscriber<ChatCompletions> subscriber = new BaseSubscriber<>() {
            @Override protected void hookOnSubscribe(Subscription subscription) { /* initially no demand */ }
            @Override protected void hookOnNext(ChatCompletions value) { received.add(value); first.countDown(); }
        };
        var result = client.getChatCompletionsStream("deployment", options());
        assertThat(requests.get()).isZero();
        result.subscribe(subscriber);
        assertThat(received).isEmpty();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        subscriber.request(1);
        assertThat(first.await(5, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
        assertThat(received).hasSize(1);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        subscriber.cancel();
        // beta.16 caches its SSE source; consumer cancellation does not reach HTTP.
        assertThat(cancelled.get()).isZero();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
    }

    @Test
    void asyncMonoCancellationReachesTransportAndEndsSpan() {
        AtomicBoolean cancelled = new AtomicBoolean();
        var client = new TracedAzureOpenAIAsyncClient(builder(
            Flux.<ByteBuffer>never().doOnCancel(() -> cancelled.set(true)), "application/json").buildAsyncClient(), tracer);
        var subscription = client.getChatCompletions("deployment", options()).subscribe();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        subscription.dispose();
        assertThat(cancelled.get()).isTrue();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
    }

    @Test
    void asyncStreamIsIncrementalAndCapturesAllTextAndTrailingUsage() {
        String second = EVENT.replace("Paris", "!").replace("\"stop\"", "null");
        String usage = "{\"id\":\"chat-stream\",\"created\":1700000000,\"choices\":[],"
            + "\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":2,\"total_tokens\":9}}";
        var client = new TracedAzureOpenAIAsyncClient(builder(
            Flux.concat(event(EVENT), event(second), sse(usage)), "text/event-stream").buildAsyncClient(), tracer);
        var result = client.getChatCompletionsStream("deployment", options(),
            new ChatCompletionStreamOptions().setIncludeUsage(true)).collectList().block(Duration.ofSeconds(5));
        assertThat(result).hasSize(3);
        assertThat(result.get(0).getChoices().get(0).getDelta().getContent()).isEqualTo("Paris");
        assertThat(result.get(1).getChoices().get(0).getDelta().getContent()).isEqualTo("!");
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("Paris!");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(9L);
    }

    @Test
    void asyncEmbeddingsAndCompletionsHaveSemanticCoverage() {
        String embedding = "{\"data\":[{\"index\":0,\"embedding\":[0.1,0.2]}],"
            + "\"usage\":{\"prompt_tokens\":7,\"total_tokens\":7}}";
        var embedded = new TracedAzureOpenAIAsyncClient(builder(bytes(embedding), "application/json").buildAsyncClient(), tracer)
            .getEmbeddings("embeddings", new EmbeddingsOptions(List.of("synthetic"))).block(Duration.ofSeconds(5));
        assertThat(embedded.getData().get(0).getEmbedding()).hasSize(2);
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
            .get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND))).isEqualTo("EMBEDDING");
        String completion = "{\"id\":\"completion\",\"created\":1700000000,"
            + "\"choices\":[{\"index\":0,\"text\":\"Paris\",\"finish_reason\":\"stop\"}]}";
        new TracedAzureOpenAIAsyncClient(builder(bytes(completion), "application/json").buildAsyncClient(), tracer)
            .getCompletions("deployment", new CompletionsOptions(List.of("capital?"))).block(Duration.ofSeconds(5));
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        assertThat(exporter.getFinishedSpanItems().get(1).getAttributes()
            .get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("Paris");
    }

    @Test
    void legacyCompletionStreamsPreserveChunksAndCaptureOutputInBothClients() {
        String completion = "{\"id\":\"completion\",\"created\":1700000000,"
            + "\"choices\":[{\"index\":0,\"text\":\"Paris\",\"finish_reason\":\"stop\"}]}";
        var options = new CompletionsOptions(List.of("capital?"));
        var sync = new TracedAzureOpenAIClient(builder(sse(completion), "text/event-stream").buildClient(), tracer);
        try (var stream = sync.getCompletionsStream("deployment", options,
                new ChatCompletionStreamOptions().setIncludeUsage(true)); var values = stream.stream()) {
            assertThat(values.toList().get(0).getChoices().get(0).getText()).isEqualTo("Paris");
        }
        var async = new TracedAzureOpenAIAsyncClient(builder(sse(completion), "text/event-stream").buildAsyncClient(), tracer);
        assertThat(async.getCompletionsStream("deployment", new CompletionsOptions(List.of("capital?")),
            new ChatCompletionStreamOptions().setIncludeUsage(true)).collectList().block(Duration.ofSeconds(5))).hasSize(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("Paris");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        });
    }

    @Test
    void transportFailuresRemainFailuresAndFinishExactlyOnce() {
        var failure = new IllegalStateException("synthetic transport failure");
        var async = new TracedAzureOpenAIAsyncClient(builder(Flux.error(failure), "application/json").buildAsyncClient(), tracer);
        assertThatThrownBy(() -> async.getChatCompletions("deployment", options()).block(Duration.ofSeconds(5)))
            .isSameAs(failure);
        var stream = new TracedAzureOpenAIAsyncClient(builder(
            Flux.concat(event(EVENT), Flux.error(failure)), "text/event-stream").buildAsyncClient(), tracer);
        assertThatThrownBy(() -> stream.getChatCompletionsStream("deployment", options())
            .collectList().block(Duration.ofSeconds(5))).isSameAs(failure);
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span ->
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR));
    }

    @Test
    void syncIteratorFailureEndsSpanAndKeepsOriginalException() {
        var failure = new IllegalStateException("synthetic transport failure");
        var client = new TracedAzureOpenAIClient(builder(
            Flux.concat(event(EVENT), Flux.error(failure)), "text/event-stream").buildClient(), tracer);
        try (var result = client.getChatCompletionsStream("deployment", options()); var stream = result.stream()) {
            assertThatThrownBy(stream::toList).isSameAs(failure);
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void streamingContentAndErrorControlsAreRespected() {
        FITracer privateTracer = new FITracer(provider.get("private"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideInputMessages(true).hideOutputMessages(true)
            .hideErrorDetails(true).build());
        var client = new TracedAzureOpenAIAsyncClient(builder(sse(EVENT), "text/event-stream").buildAsyncClient(), privateTracer);
        client.getChatCompletionsStream("deployment", options()).collectList().block(Duration.ofSeconds(5));
        var failure = new IllegalStateException("private-error");
        var errors = new TracedAzureOpenAIAsyncClient(builder(Flux.error(failure), "application/json").buildAsyncClient(), privateTracer);
        assertThatThrownBy(() -> errors.getChatCompletions("deployment", options()).block(Duration.ofSeconds(5))).isSameAs(failure);
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getAttributes().toString()).doesNotContain("Paris", "capital?", "private-error");
            assertThat(span.getEvents().toString()).doesNotContain("private-error");
            assertThat(span.getStatus().getDescription()).isEmpty();
        });
    }

    @Test
    void accumulatedStreamTextIsBoundedWithoutChangingChunks() {
        String large = EVENT.replace("Paris", "x".repeat(40000));
        var client = new TracedAzureOpenAIAsyncClient(builder(sse(large), "text/event-stream").buildAsyncClient(), tracer);
        var chunks = client.getChatCompletionsStream("deployment", options()).collectList().block(Duration.ofSeconds(5));
        assertThat(chunks.get(0).getChoices().get(0).getDelta().getContent()).hasSize(40000);
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
            .get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).hasSize(32000);
    }

    @Test
    @Timeout(5)
    void nativeBeta16AlsoKeepsCachedHttpBodyAfterConsumerClose() {
        AtomicInteger cancelled = new AtomicInteger();
        var sdk = builder(Flux.concat(event(EVENT), Flux.<ByteBuffer>never())
            .doOnCancel(cancelled::incrementAndGet), "text/event-stream").buildClient();
        try (var stream = sdk.getChatCompletionsStream("deployment", options()).stream()) {
            assertThat(stream.findFirst()).isPresent();
        }
        assertThat(cancelled.get()).isZero();
        var async = builder(Flux.concat(event(EVENT), Flux.<ByteBuffer>never())
            .doOnCancel(cancelled::incrementAndGet), "text/event-stream").buildAsyncClient();
        assertThat(async.getChatCompletionsStream("deployment", options()).next().block(Duration.ofSeconds(5)))
            .isNotNull();
        assertThat(cancelled.get()).isZero();
    }

    @Test
    void syncWrapperClosesSdkConsumerOnceWithoutExtraSubscriptions() {
        AtomicInteger subscribed = new AtomicInteger();
        AtomicInteger cancelled = new AtomicInteger();
        var source = new IterableStream<>(Flux.<ChatCompletions>never()
            .doOnSubscribe(ignored -> subscribed.incrementAndGet()).doOnCancel(cancelled::incrementAndGet));
        var trace = new AzureOpenAITrace(tracer, "Chat Completion", ai.traceai.FISpanKind.LLM,
            io.opentelemetry.context.Context.current());
        try (var result = new TracedAzureOpenAIStream<>(source, trace, trace::chunk)) {
            assertThat(subscribed.get()).isZero();
            try (var stream = result.stream()) { assertThat(subscribed.get()).isEqualTo(1); }
        }
        assertThat(cancelled.get()).isEqualTo(1);
        assertThat(subscribed.get()).isEqualTo(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void synchronousSdkSetupErrorsAreNotReplaced() {
        var failure = new IllegalArgumentException("invalid deployment");
        var options = options();
        var sdk = builder(bytes(CHAT), "application/json")
            .httpClient(request -> Mono.error(failure)).buildClient();
        assertThatThrownBy(() -> new TracedAzureOpenAIClient(sdk, tracer)
            .getChatCompletionsStream("deployment", options)).isSameAs(failure);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void callbackFailuresCannotTurnAResponseIntoAnApplicationFailure() {
        FITracer broken = new FITracer(provider.get("broken")) {
            @Override public void setOutputValue(Span span, String value) {
                throw new IllegalStateException("telemetry-only failure");
            }
        };
        var client = new TracedAzureOpenAIAsyncClient(builder(bytes(CHAT), "application/json").buildAsyncClient(), broken);
        assertThat(client.getChatCompletions("deployment", options()).block(Duration.ofSeconds(5)))
            .isNotNull();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void toolCallDeltasArePassedThroughWithoutInventingAggregatedArguments() {
        String tool = EVENT.replace("\"content\":\"Paris\"",
            "\"tool_calls\":[{\"index\":0,\"id\":\"call-test\",\"type\":\"function\","
                + "\"function\":{\"name\":\"weather\",\"arguments\":\"{}\"}}]");
        var client = new TracedAzureOpenAIAsyncClient(builder(sse(tool), "text/event-stream").buildAsyncClient(), tracer);
        var chunks = client.getChatCompletionsStream("deployment", options()).collectList().block(Duration.ofSeconds(5));
        var call = (ChatCompletionsFunctionToolCall) chunks.get(0).getChoices().get(0).getDelta().getToolCalls().get(0);
        assertThat(call.getId()).isEqualTo("call-test");
        assertThat(call.getFunction().getName()).isEqualTo("weather");
        assertThat(call.getFunction().getArguments()).isEqualTo("{}");
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
            .get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON))).isEqualTo("stop");
    }

    private Flux<ByteBuffer> bytes(String value) {
        return Flux.defer(() -> Flux.just(ByteBuffer.wrap(value.getBytes(StandardCharsets.UTF_8))));
    }

    private Flux<ByteBuffer> event(String value) { return bytes("data: " + value + "\n\n"); }

    private void assertAzureSpan() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_PROVIDER))).isEqualTo("azure");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("Paris");
    }

    private ChatCompletionsOptions options() {
        return new ChatCompletionsOptions(List.of(new ChatRequestUserMessage("capital?")));
    }

    private Flux<ByteBuffer> sse(String event) {
        return bytes("data: " + event + "\n\ndata: [DONE]\n\n");
    }

    private OpenAIClientBuilder builder(Flux<ByteBuffer> body, String contentType) {
        return new OpenAIClientBuilder().endpoint("https://synthetic.openai.azure.com")
            .credential(new AzureKeyCredential("synthetic-not-a-secret"))
            .retryOptions(new RetryOptions(new FixedDelayOptions(0, Duration.ofMillis(1))))
            .httpClient(request -> Mono.defer(() -> {
                requests.incrementAndGet();
                return Mono.just(new HttpResponse(request) {
                    private final HttpHeaders headers = new HttpHeaders().set("Content-Type", contentType);
                    @Override public int getStatusCode() { return 200; }
                    @Override public String getHeaderValue(String name) { return headers.getValue(name); }
                    @Override public HttpHeaders getHeaders() { return headers; }
                    @Override public Flux<ByteBuffer> getBody() { return body; }
                    @Override public Mono<byte[]> getBodyAsByteArray() {
                        return body.map(buffer -> { byte[] bytes = new byte[buffer.remaining()]; buffer.get(bytes); return bytes; })
                            .reduce(new byte[0], (a, b) -> {
                                byte[] bytes = java.util.Arrays.copyOf(a, a.length + b.length);
                                System.arraycopy(b, 0, bytes, a.length, b.length); return bytes;
                            });
                    }
                    @Override public Mono<String> getBodyAsString() { return getBodyAsString(StandardCharsets.UTF_8); }
                    @Override public Mono<String> getBodyAsString(Charset charset) {
                        return getBodyAsByteArray().map(bytes -> new String(bytes, charset));
                    }
                });
            }));
    }
}
