package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.reactivestreams.Subscriber;
import org.reactivestreams.Subscription;
import software.amazon.awssdk.core.async.SdkPublisher;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;

import static org.assertj.core.api.Assertions.*;

class BedrockConverseStreamTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private FITracer tracer(boolean hide) {
        return new FITracer(otel.getOpenTelemetry().getTracer("stream-test"), TraceConfig.builder()
            .hideInputs(hide).hideOutputs(hide).hideErrorDetails(true).build());
    }

    private ConverseStreamRequest request() {
        return ConverseStreamRequest.builder().modelId("amazon.nova-pro-v1:0").build();
    }

    private BedrockRuntimeAsyncClient sdk(Function<ConverseStreamResponseHandler, CompletableFuture<Void>> call) {
        return (BedrockRuntimeAsyncClient) Proxy.newProxyInstance(BedrockRuntimeAsyncClient.class.getClassLoader(),
            new Class<?>[]{BedrockRuntimeAsyncClient.class}, (proxy, method, arguments) -> {
                if (method.getName().equals("converseStream")) return call.apply((ConverseStreamResponseHandler) arguments[1]);
                throw new UnsupportedOperationException(method.getName());
            });
    }

    @Test
    void preservesDemandEventIdentityAndResponseIdentity() {
        var sdkFuture = new CompletableFuture<Void>();
        var captured = new AtomicReference<ConverseStreamResponseHandler>();
        var client = new TracedBedrockRuntimeAsyncClient(sdk(handler -> { captured.set(handler); return sdkFuture; }), tracer(false));
        List<Long> demand = new ArrayList<>();
        List<ConverseStreamOutput> events = new ArrayList<>();
        AtomicReference<ConverseStreamResponse> headers = new AtomicReference<>();
        AtomicReference<Subscription> downstream = new AtomicReference<>();
        var handler = ConverseStreamResponseHandler.builder().onResponse(headers::set).subscriber(() -> new Subscriber<ConverseStreamOutput>() {
            public void onSubscribe(Subscription subscription) { downstream.set(subscription); }
            public void onNext(ConverseStreamOutput event) { events.add(event); }
            public void onError(Throwable error) { throw new AssertionError(error); }
            public void onComplete() { }
        }).build();
        var future = client.converseStream(request(), handler);
        ConverseStreamResponse response = ConverseStreamResponse.builder().build();
        captured.get().responseReceived(response);
        assertThat(headers.get()).isSameAs(response);
        assertThat(otel.getSpans()).isEmpty();
        AtomicReference<Subscriber<? super ConverseStreamOutput>> subscriber = new AtomicReference<>();
        captured.get().onEventStream(SdkPublisher.adapt(value -> {
            subscriber.set(value);
            value.onSubscribe(new Subscription() {
                public void request(long n) { demand.add(n); }
                public void cancel() { }
            });
        }));
        assertThat(demand).isEmpty();
        downstream.get().request(1);
        downstream.get().request(3);
        assertThat(demand).containsExactly(1L, 3L);
        var event = ContentBlockDeltaEvent.builder().contentBlockIndex(0).delta(ContentBlockDelta.builder().text("answer").build()).build();
        subscriber.get().onNext(event);
        assertThat(events).singleElement().isSameAs(event);
        subscriber.get().onComplete();
        assertThat(otel.getSpans()).isEmpty();
        captured.get().complete();
        sdkFuture.complete(null);
        assertThat(future).isCompletedWithValue(null);
        // Duplicate/late notifications must neither export a second span nor replace success.
        captured.get().exceptionOccurred(new IllegalStateException("late"));
        captured.get().complete();
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(otel.getSpans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(otel.getSpans().get(0).getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            .isEqualTo("answer");
    }

    @Test
    void cancellationBeforeSdkFutureAttachmentIsNotLost() {
        CompletableFuture<Void> sdkFuture = new CompletableFuture<>();
        AtomicInteger cancelled = new AtomicInteger();
        var client = new TracedBedrockRuntimeAsyncClient(sdk(handler -> {
            handler.onEventStream(SdkPublisher.adapt(subscriber -> subscriber.onSubscribe(new Subscription() {
                public void request(long n) { throw new AssertionError("must not request eagerly"); }
                public void cancel() { cancelled.incrementAndGet(); }
            })));
            return sdkFuture;
        }), tracer(false));
        var handler = ConverseStreamResponseHandler.builder().subscriber(() -> new Subscriber<ConverseStreamOutput>() {
            public void onSubscribe(Subscription subscription) { subscription.cancel(); }
            public void onNext(ConverseStreamOutput event) { throw new AssertionError("must not deliver after cancellation"); }
            public void onError(Throwable error) { }
            public void onComplete() { }
        }).build();
        var result = client.converseStream(request(), handler);
        assertThat(cancelled.get()).isEqualTo(1);
        assertThat(sdkFuture.isCancelled()).isTrue();
        assertThat(result.isCancelled()).isTrue();
        assertThat(otel.getSpans()).hasSize(1);
        assertThat(otel.getSpans().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void accumulationIsBoundedAndContentHidingKeepsUsage(boolean hide) {
        var tracing = new BedrockTracing(tracer(hide));
        var span = tracing.start(request());
        var output = new BedrockStreamOutput(tracing, span);
        for (int i = 0; i < 200; i++) output.accept(ContentBlockDeltaEvent.builder().contentBlockIndex(i)
            .delta(ContentBlockDelta.builder().text("private-output".repeat(1000)).build()).build());
        output.accept(ConverseStreamMetadataEvent.builder().usage(TokenUsage.builder()
            .inputTokens(2).outputTokens(3).totalTokens(5).build()).build());
        output.capture();
        span.end();
        var data = otel.getSpans().get(0);
        assertThat(data.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(5);
        if (hide) {
            assertThat(data.getAttributes().toString()).doesNotContain("private-output");
            assertThat(data.getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_OUTPUT))).isNull();
        } else {
            assertThat(data.getAttributes().get(AttributeKey.booleanKey("gen_ai.bedrock.output_truncated"))).isTrue();
            assertThat(data.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).hasSizeLessThanOrEqualTo(32128);
            assertThat(data.getAttributes().get(AttributeKey.stringKey(SemanticConventions.RAW_OUTPUT))).hasSizeLessThanOrEqualTo(32000);
        }
    }
}
