package ai.traceai.semantickernel;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import com.microsoft.semantickernel.services.chatcompletion.ChatHistory;
import com.microsoft.semantickernel.services.chatcompletion.StreamingChatContent;
import com.microsoft.semantickernel.services.textembedding.Embedding;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class ReactiveLifecycleTest {
    private static final AttributeKey<Boolean> CANCELLED = AttributeKey.booleanKey("semantic_kernel.cancelled");
    InMemorySpanExporter exporter;
    SdkTracerProvider sdk;
    FITracer tracer;
    AtomicInteger started;
    TextEmbeddingGenerationService delegate;
    TracedTextEmbeddingGenerationService service;

    @BeforeEach void setup() {
        exporter = InMemorySpanExporter.create();
        started = new AtomicInteger();
        sdk = SdkTracerProvider.builder()
            .addSpanProcessor(new SpanProcessor() {
                public void onStart(Context parent, ReadWriteSpan span) { started.incrementAndGet(); }
                public boolean isStartRequired() { return true; }
                public void onEnd(ReadableSpan span) { }
                public boolean isEndRequired() { return false; }
            })
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(sdk.get("test"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
        delegate = mock(TextEmbeddingGenerationService.class);
        service = new TracedTextEmbeddingGenerationService(delegate, "embedding-model", "test", tracer);
    }

    @AfterEach void close() { sdk.close(); }

    @Test void doesNotStartOrCallDelegateWithoutSubscription() {
        when(delegate.generateEmbeddingsAsync(any())).thenReturn(Mono.just(List.of()));
        service.generateEmbeddingsAsync(List.of("private-input"));
        assertThat(started).hasValue(0);
        verifyNoInteractions(delegate);
    }

    @Test void resubscriptionCreatesIndependentSpans() {
        when(delegate.generateEmbeddingsAsync(any())).thenReturn(Mono.just(List.of(new Embedding(List.of(1f)))));
        Mono<List<Embedding>> result = service.generateEmbeddingsAsync(List.of("private-input"));
        result.block();
        result.block();
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        assertThat(exporter.getFinishedSpanItems().get(0).getSpanId())
            .isNotEqualTo(exporter.getFinishedSpanItems().get(1).getSpanId());
        verify(delegate, times(2)).generateEmbeddingsAsync(any());
    }

    @Test void delegateSeesSpanDuringDeferredSubscriptionAndParentRestored() {
        AtomicReference<String> current = new AtomicReference<>();
        when(delegate.generateEmbeddingsAsync(any())).thenReturn(Mono.defer(() -> {
            current.set(Span.current().getSpanContext().getSpanId());
            return Mono.just(List.of());
        }));
        Mono<List<Embedding>> result = service.generateEmbeddingsAsync(List.of("private-input"));
        Span parent = sdk.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            result.block();
            assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
        }
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(current.get()).isEqualTo(span.getSpanId());
        parent.end();
    }

    @Test void synchronousDelegateThrowEndsSpanAndPreservesError() {
        var error = new IllegalStateException("private-error");
        when(delegate.generateEmbeddingsAsync(any())).thenThrow(error);
        Mono<List<Embedding>> result = service.generateEmbeddingsAsync(List.of("private-input"));
        assertThatThrownBy(result::block).isSameAs(error);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(exporter.getFinishedSpanItems().get(0).toString()).doesNotContain("private-error");
    }

    @Test void inputHidingAlsoHidesIndexedEmbeddingText() {
        when(delegate.generateEmbeddingsAsync(any())).thenReturn(Mono.just(List.of()));
        service.generateEmbeddingsAsync(List.of("private-input")).block();
        assertThat(exporter.getFinishedSpanItems().toString()).doesNotContain("private-input");
    }

    @Test void cancellationEndsEmbeddingSpanWithoutSuccess() {
        when(delegate.generateEmbeddingAsync(anyString())).thenReturn(Mono.never());
        var subscription = service.generateEmbeddingAsync("private-input").subscribe();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        subscription.dispose();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isNotEqualTo(StatusCode.OK);
    }

    @Test void streamingIsLazyAndResubscribable() {
        ChatCompletionService chat = mock(ChatCompletionService.class);
        when(chat.getStreamingChatMessageContentsAsync(any(ChatHistory.class), isNull(), any()))
            .thenReturn(Flux.empty());
        TracedChatCompletionService traced = new TracedChatCompletionService(chat, "model", "test", tracer);
        Flux<StreamingChatContent<?>> stream = traced.getStreamingChatMessageContentsAsync(new ChatHistory(), null, null);
        assertThat(started).hasValue(0);
        stream.blockLast();
        stream.blockLast();
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @Test void streamingCancellationEndsSpanOnceWithPartialOutputHidden() {
        ChatCompletionService chat = mock(ChatCompletionService.class);
        AtomicInteger cancellations = new AtomicInteger();
        when(chat.getStreamingChatMessageContentsAsync(any(ChatHistory.class), isNull(), any()))
            .thenReturn(Flux.<StreamingChatContent<?>>never().doOnCancel(cancellations::incrementAndGet));
        TracedChatCompletionService traced = new TracedChatCompletionService(chat, "model", "test", tracer);
        var subscription = traced.getStreamingChatMessageContentsAsync(new ChatHistory(), null, null).subscribe();
        subscription.dispose();
        subscription.dispose();
        assertThat(cancellations).hasValue(1);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes().get(CANCELLED)).isTrue();
    }
    @Test void contextSurvivesSchedulerHopsWithoutLeakingToCaller() {
        AtomicReference<String> current = new AtomicReference<>();
        when(delegate.generateEmbeddingsAsync(any())).thenReturn(
            Mono.delay(java.time.Duration.ofMillis(5)).map(ignored -> {
                current.set(Span.current().getSpanContext().getSpanId());
                return List.of();
            }));
        Span parent = sdk.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            service.generateEmbeddingsAsync(List.of("private-input")).block();
            assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
        }
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(current.get()).isEqualTo(span.getSpanId());
        parent.end();
    }

    @Test void concurrentSubscriptionsKeepIndependentContexts() {
        when(delegate.generateEmbeddingsAsync(any())).thenAnswer(call ->
            Mono.delay(java.time.Duration.ofMillis(5)).map(ignored -> {
                Span.current().setAttribute("app.request", (String) ((List<?>) call.getArgument(0)).get(0));
                return List.of();
            }));
        Flux.range(0, 20).flatMap(i -> service.generateEmbeddingsAsync(List.of("request-" + i)))
            .blockLast();
        assertThat(exporter.getFinishedSpanItems()).hasSize(20);
        assertThat(exporter.getFinishedSpanItems().stream().map(span ->
            span.getAttributes().get(AttributeKey.stringKey("app.request"))))
            .doesNotContainNull().doesNotHaveDuplicates();
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test void lateNativeSpanAfterCancellationIsEndedImmediately() {
        Span span = tracer.startSpan("invocation", ai.traceai.FISpanKind.AGENT);
        TracedInvocation invocation = new TracedInvocation(tracer, span, Context.root().with(span));
        invocation.finish(reactor.core.publisher.SignalType.CANCEL);
        invocation.telemetry().spanBuilder("chat.completions late").startSpan();
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        assertThat(exporter.getFinishedSpanItems().get(1).getAttributes().get(CANCELLED)).isTrue();
    }

    @Test void streamingPreservesChunkIdentityAndBoundsCapturedText() {
        ChatCompletionService chat = mock(ChatCompletionService.class);
        StreamingChatContent<?> chunk = mock(StreamingChatContent.class);
        when(chunk.getContent()).thenReturn("x".repeat(100000));
        when(chat.getStreamingChatMessageContentsAsync(any(ChatHistory.class), isNull(), any()))
            .thenReturn(Flux.just(chunk));
        FITracer contentTracer = new FITracer(sdk.get("test"), TraceConfig.builder().build());
        var traced = new TracedChatCompletionService(chat, "model", "test", contentTracer);
        var result = traced.getStreamingChatMessageContentsAsync(new ChatHistory(), null, null).collectList().block();
        assertThat(result).containsExactly(chunk);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().get(AttributeKey.stringKey("output.value"))).hasSize(32000);
        assertThat(span.getAttributes().get(AttributeKey.booleanKey("semantic_kernel.output_truncated"))).isTrue();
    }

    @Test void customChatServiceRetainsUsageWithoutCountingSharedMetadataTwice() {
        var usage = java.util.Map.of("promptTokens", 7, "completionTokens", 3, "totalTokens", 10);
        var metadata = com.microsoft.semantickernel.orchestration.FunctionResultMetadata.build(
            "custom-response", usage, java.time.OffsetDateTime.now());
        var message = new com.microsoft.semantickernel.services.chatcompletion.ChatMessageContent<>(
            com.microsoft.semantickernel.services.chatcompletion.AuthorRole.ASSISTANT,
            "private-answer", "model", null, null, metadata);
        ChatCompletionService chat = mock(ChatCompletionService.class);
        when(chat.getChatMessageContentsAsync(any(ChatHistory.class), isNull(), any()))
            .thenReturn(Mono.just(List.of(message, message)));
        new TracedChatCompletionService(chat, "model", "custom", tracer)
            .getChatMessageContentsAsync(new ChatHistory(), null, null).block();
        var spans = exporter.getFinishedSpanItems();
        assertThat(spans).hasSize(1);
        assertThat(spans.get(0).getAttributes().get(AttributeKey.stringKey("fi.span.kind"))).isEqualTo("LLM");
        assertThat(spans.get(0).getAttributes().get(AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(10L);
        assertThat(spans.toString()).doesNotContain("private-answer");
    }

}
