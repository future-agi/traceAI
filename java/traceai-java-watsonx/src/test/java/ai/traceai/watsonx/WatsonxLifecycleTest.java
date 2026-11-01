package ai.traceai.watsonx;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.ibm.watsonx.ai.chat.ChatHandler;
import com.ibm.watsonx.ai.chat.ChatRequest;
import com.ibm.watsonx.ai.chat.ChatService;
import com.ibm.watsonx.ai.chat.model.CompletedToolCall;
import com.ibm.watsonx.ai.chat.model.PartialChatResponse;
import com.ibm.watsonx.ai.chat.model.PartialToolCall;
import com.ibm.watsonx.ai.textgeneration.TextGenerationHandler;
import com.ibm.watsonx.ai.textgeneration.TextGenerationRequest;
import com.ibm.watsonx.ai.textgeneration.TextGenerationResponse;
import com.ibm.watsonx.ai.textgeneration.TextGenerationService;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

class WatsonxLifecycleTest {
    InMemorySpanExporter exporter;
    SdkTracerProvider provider;
    FITracer tracer;
    TextGenerationService service;
    TextGenerationRequest request;
    TextGenerationResponse response;
    CompletableFuture<Void> future;
    CompletableFuture<Void> tracedFuture;
    ArgumentCaptor<TextGenerationHandler> callbacks;

    @BeforeEach
    void setup() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("test"), TraceConfig.builder().build());
        service = mock(TextGenerationService.class);
        request = TextGenerationRequest.builder().input("private input").build();
        response = new TextGenerationResponse("model", "time", "version", List.of(
            new TextGenerationResponse.Result("answer", "eos_token", 2, 3, null, null, null, null)));
        future = new CompletableFuture<>();
        callbacks = ArgumentCaptor.forClass(TextGenerationHandler.class);
        when(service.generateStreaming(eq(request), any())).thenReturn(future);
    }

    @AfterEach void cleanup() { provider.close(); }

    TextGenerationHandler start(TextGenerationHandler user) {
        tracedFuture = new TracedWatsonxAI(service, tracer).generateTextStream(request, user);
        verify(service).generateStreaming(eq(request), callbacks.capture());
        return callbacks.getValue();
    }

    @Test
    void spanStaysOpenUntilFutureCompletesAndKeepsParentAndCallbackContext() throws Exception {
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        ContextKey<String> key = ContextKey.named("callback-context");
        AtomicReference<String> seen = new AtomicReference<>();
        AtomicReference<String> callbackSpan = new AtomicReference<>();
        TextGenerationHandler user = mock(TextGenerationHandler.class);
        doAnswer(invocation -> {
            seen.set(Context.current().get(key));
            callbackSpan.set(Span.current().getSpanContext().getSpanId());
            return null;
        }).when(user).onPartialResponse("partial");
        TextGenerationHandler handler;
        try (Scope ignored = Context.current().with(parent).with(key, "preserved").makeCurrent()) {
            handler = start(user);
            assertThat(Span.current()).isSameAs(parent);
        }
        CompletableFuture.runAsync(() -> handler.onPartialResponse("partial")).get();
        assertThat(seen).hasValue("preserved");
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        handler.onCompleteResponse(response);
        // SDK completion callbacks finish before the future settles; do not end early.
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        future.complete(null);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(callbackSpan).hasValue(span.getSpanId());
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(5);
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("answer");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(future.complete(null)).isFalse();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        verify(user).onCompleteResponse(response);
        parent.end();
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void nonTerminalErrorDoesNotEndSpanOrChangeFailOnFirstError() {
        TextGenerationHandler user = mock(TextGenerationHandler.class);
        TextGenerationHandler handler = start(user);
        assertThat(handler.failOnFirstError()).isFalse();
        IllegalStateException error = new IllegalStateException("transient");
        handler.onError(error);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        handler.onCompleteResponse(response);
        future.complete(null);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        verify(user).onError(error);
    }

    @Test
    void terminalFutureErrorIsRecordedEvenWithoutErrorCallback() {
        start(mock(TextGenerationHandler.class));
        IllegalStateException error = new IllegalStateException("failure");
        future.completeExceptionally(error);
        assertThatThrownBy(tracedFuture::join).hasCause(error);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void cancellationReachesOriginalFutureAndEndsExactlyOnce() {
        TextGenerationHandler handler = start(mock(TextGenerationHandler.class));
        assertThat(tracedFuture.cancel(true)).isTrue();
        assertThat(tracedFuture.isCancelled()).isTrue();
        handler.onCompleteResponse(response); // Late callback cannot create a second span.
        assertThat(future.isCancelled()).isTrue();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void synchronousSdkErrorIsUnchangedAndEndsSpan() {
        IllegalArgumentException error = new IllegalArgumentException("bad request");
        when(service.generateStreaming(eq(request), any())).thenThrow(error);
        assertThatThrownBy(() -> new TracedWatsonxAI(service, tracer).generateTextStream(request, mock(TextGenerationHandler.class)))
            .isSameAs(error);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void synchronousGenerationErrorIsUnchanged() {
        IllegalArgumentException error = new IllegalArgumentException("bad request");
        when(service.generate(request)).thenThrow(error);
        assertThatThrownBy(() -> new TracedWatsonxAI(service, tracer).generateText(request)).isSameAs(error);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void callbackExceptionIsNotSwallowed() {
        TextGenerationHandler user = mock(TextGenerationHandler.class);
        IllegalArgumentException error = new IllegalArgumentException("callback failed");
        doThrow(error).when(user).onPartialResponse("chunk");
        TextGenerationHandler handler = start(user);
        assertThatThrownBy(() -> handler.onPartialResponse("chunk")).isSameAs(error);
        future.completeExceptionally(error);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void chatForwardsEveryCallbackAndCancellation() {
        ChatService chat = mock(ChatService.class);
        ChatRequest request = mock(ChatRequest.class);
        ChatHandler user = mock(ChatHandler.class);
        when(user.failOnFirstError()).thenReturn(true);
        when(chat.chatStreaming(eq(request), any())).thenAnswer(invocation -> future);
        CompletableFuture<?> traced = new TracedWatsonxAI(chat, tracer).chatStream(request, user);
        ArgumentCaptor<ChatHandler> capture = ArgumentCaptor.forClass(ChatHandler.class);
        verify(chat).chatStreaming(eq(request), capture.capture());
        ChatHandler handler = capture.getValue();
        PartialChatResponse partial = mock(PartialChatResponse.class);
        PartialToolCall tool = mock(PartialToolCall.class);
        CompletedToolCall complete = mock(CompletedToolCall.class);
        handler.onPartialResponse("chunk", partial);
        handler.onPartialThinking("reasoning", partial);
        handler.onPartialToolCall(tool);
        handler.onCompleteToolCall(complete);
        assertThat(handler.failOnFirstError()).isTrue();
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        future.cancel(true);
        assertThat(traced.isCancelled()).isTrue();
        verify(user).onPartialResponse("chunk", partial);
        verify(user).onPartialThinking("reasoning", partial);
        verify(user).onPartialToolCall(tool);
        verify(user).onCompleteToolCall(complete);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void returnedFutureCompletesOnlyAfterSpanEnds() throws Exception {
        var entered = new java.util.concurrent.CountDownLatch(1);
        var release = new java.util.concurrent.CountDownLatch(1);
        Span span = spy(provider.get("test").spanBuilder("blocking-end").startSpan());
        doAnswer(invocation -> {
            entered.countDown();
            if (!release.await(5, java.util.concurrent.TimeUnit.SECONDS)) throw new IllegalStateException("test timed out");
            return invocation.callRealMethod();
        }).when(span).end();
        FITracer tracer = spy(this.tracer);
        doReturn(span).when(tracer).startSpan(any(), any());
        CompletableFuture<Void> returned = new TracedWatsonxAI(service, tracer)
            .generateTextStream(request, mock(TextGenerationHandler.class));
        CompletableFuture<?> producer = CompletableFuture.runAsync(() -> future.complete(null));
        try {
            assertThat(entered.await(5, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
            assertThat(future.isDone()).isTrue();
            assertThat(returned.isDone()).isFalse();
        } finally { release.countDown(); }
        returned.get(5, java.util.concurrent.TimeUnit.SECONDS);
        producer.get(5, java.util.concurrent.TimeUnit.SECONDS);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void telemetryFailureDoesNotReplaceSdkResponse() {
        FITracer brokenTracer = spy(tracer);
        doThrow(new IllegalStateException("telemetry unavailable")).when(brokenTracer).setRawInput(any(), any());
        when(service.generate(request)).thenReturn(response);
        assertThat(new TracedWatsonxAI(service, brokenTracer).generateText(request)).isSameAs(response);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void hideInputsDoesNotLeakGenerationInputTokensThroughOutput() {
        FITracer privateTracer = new FITracer(provider.get("private"), TraceConfig.builder().hideInputs(true).build());
        var token = new TextGenerationResponse.TokenInfo("private input", null, null, null);
        var response = new TextGenerationResponse("model", "time", "version", List.of(
            new TextGenerationResponse.Result("answer", "eos_token", 2, 3, null, null, List.of(token), null)));
        when(service.generate(request)).thenReturn(response);
        new TracedWatsonxAI(service, privateTracer).generateText(request);
        var span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getAttributes().toString()).doesNotContain("private input");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("answer");
    }

    @Test
    void alreadyCompletedFutureStillExportsOneSpan() {
        future.complete(null);
        start(mock(TextGenerationHandler.class));
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }
}
