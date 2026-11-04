package ai.traceai.bedrock;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import org.reactivestreams.Subscriber;
import org.reactivestreams.Subscription;
import software.amazon.awssdk.core.async.SdkPublisher;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamOutput;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamRequest;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamResponse;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamResponseHandler;

import java.util.Objects;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/** One span per invocation, not per response header or event-stream subscription. */
final class BedrockConverseStream implements ConverseStreamResponseHandler {
    private final BedrockTracing tracing;
    private final ConverseStreamRequest request;
    private final ConverseStreamResponseHandler delegate;
    private final Span span;
    private final Context context;
    private final BedrockStreamOutput output;
    private final AtomicReference<CompletableFuture<Void>> sdkFuture = new AtomicReference<>();
    private final AtomicBoolean subscriptionCancelled = new AtomicBoolean();
    private boolean finished;

    BedrockConverseStream(BedrockTracing tracing, ConverseStreamRequest request, ConverseStreamResponseHandler delegate) {
        this.tracing = tracing;
        this.request = Objects.requireNonNull(request, "request");
        this.delegate = Objects.requireNonNull(delegate, "handler");
        span = tracing.start(request);
        context = Context.current().with(span).with(BedrockTracing.WRAPPED_REQUEST, request);
        output = new BedrockStreamOutput(tracing, span);
    }

    CompletableFuture<Void> execute(BedrockRuntimeAsyncClient client) {
        CompletableFuture<Void> sdk;
        try (Scope ignored = context.makeCurrent()) {
            sdk = client.converseStream(request, this);
        } catch (RuntimeException | Error error) {
            finish(error);
            throw error;
        }
        sdkFuture.set(sdk);
        class TracedFuture extends CompletableFuture<Void> {
            @Override public boolean cancel(boolean interrupt) { return sdk.cancel(interrupt); }
            @Override public boolean complete(Void value) { return sdk.complete(value); }
            @Override public boolean completeExceptionally(Throwable error) { return sdk.completeExceptionally(error); }
            void publish(Throwable error) {
                if (error == null) super.complete(null);
                else super.completeExceptionally(error);
            }
        }
        TracedFuture result = new TracedFuture();
        sdk.whenComplete((value, error) -> {
            try {
                finish(error);
            } finally {
                // All application future callbacks see a finished span, including timeouts/cancellation.
                result.publish(error);
            }
        });
        // A synchronous publisher may cancel before client.converseStream returns its future.
        if (subscriptionCancelled.get()) sdk.cancel(false);
        return result;
    }

    @Override
    public void responseReceived(ConverseStreamResponse response) {
        callback(() -> delegate.responseReceived(response));
        // Headers are not the end of an event stream.
    }

    @Override
    public void onEventStream(SdkPublisher<ConverseStreamOutput> publisher) {
        callback(() -> delegate.onEventStream(SdkPublisher.adapt(subscriber -> publisher.subscribe(new Subscriber<ConverseStreamOutput>() {
            @Override
            public void onSubscribe(Subscription subscription) {
                callback(() -> subscriber.onSubscribe(new Subscription() {
                    @Override public void request(long n) {
                        // No eager requests, buffering, replacement events, or changed demand.
                        subscription.request(n);
                    }
                    @Override public void cancel() {
                        subscriptionCancelled.set(true);
                        finish(new CancellationException("Bedrock stream subscription cancelled"));
                        try {
                            subscription.cancel();
                        } finally {
                            CompletableFuture<Void> sdk = sdkFuture.get();
                            if (sdk != null) sdk.cancel(false);
                        }
                    }
                }));
            }
            @Override public void onNext(ConverseStreamOutput event) {
                accept(event);
                callback(() -> subscriber.onNext(event));
            }
            @Override public void onError(Throwable error) {
                finish(error);
                callback(() -> subscriber.onError(error));
            }
            @Override public void onComplete() {
                callback(subscriber::onComplete);
            }
        }))));
    }

    @Override
    public void exceptionOccurred(Throwable error) {
        finish(error);
        callback(() -> delegate.exceptionOccurred(error));
    }

    @Override
    public void complete() {
        callback(delegate::complete);
        finish(null);
    }

    private void callback(Runnable operation) {
        try (Scope ignored = context.makeCurrent()) {
            operation.run();
        } catch (RuntimeException | Error error) {
            finish(error);
            throw error;
        }
    }

    private synchronized void accept(ConverseStreamOutput event) {
        if (finished) return;
        try {
            output.accept(event);
        } catch (RuntimeException ignored) {
            // New/unknown event shapes cannot prevent delivery to the application.
        }
    }

    private synchronized void finish(Throwable error) {
        if (finished) return;
        finished = true;
        try {
            output.capture();
            if (error == null) span.setStatus(StatusCode.OK);
            else tracing.fail(span, error instanceof CompletionException && error.getCause() != null ? error.getCause() : error);
        } catch (RuntimeException ignored) {
            // Telemetry must not replace the SDK's terminal outcome.
        } finally {
            span.end();
        }
    }
}
