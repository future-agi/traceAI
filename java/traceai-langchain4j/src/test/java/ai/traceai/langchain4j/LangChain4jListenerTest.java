package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.chat.listener.*;
import dev.langchain4j.model.output.FinishReason;
import dev.langchain4j.model.output.Response;
import dev.langchain4j.model.output.TokenUsage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import static org.assertj.core.api.Assertions.*;

class LangChain4jListenerTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider sdk = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final FITracer tracer = new FITracer(sdk.get("test"));
    private final ChatModelRequest request = ChatModelRequest.builder().model("test-model")
            .messages(List.of(UserMessage.from("private-input"))).build();
    private final ChatModelResponse response = ChatModelResponse.builder().model("resolved-model")
            .id("response-id").aiMessage(AiMessage.from(ToolExecutionRequest.builder()
                    .id("private-tool-id").name("private-tool-name").arguments("private-tool-args").build()))
            .tokenUsage(new TokenUsage(5, 3)).finishReason(FinishReason.TOOL_EXECUTION).build();
    @AfterEach void close() { sdk.close(); }

    @Test void defaultsToMetadataOnlyAndFinishesExactlyOnceAcrossThreads() throws Exception {
        LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic");
        Map<Object, Object> attributes = new ConcurrentHashMap<>();
        Span parent = sdk.get("test").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            listener.onRequest(new ChatModelRequestContext(request, attributes));
            // A repeated registration must not create a second span.
            new LangChain4jListener(tracer, "wrong-provider", true)
                    .onRequest(new ChatModelRequestContext(request, attributes));
            assertThat(Span.current()).isSameAs(parent);
        }
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        java.util.concurrent.CompletableFuture.runAsync(() -> {
            listener.onResponse(new ChatModelResponseContext(response, request, attributes));
            listener.onError(new ChatModelErrorContext(new RuntimeException("late"), request, null, attributes));
            assertThat(Span.current().getSpanContext().isValid()).isFalse();
        }).join();
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().toString()).doesNotContain("private-");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL)))
                    .isEqualTo("test-model");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_MODEL)))
                    .isEqualTo("resolved-model");
            assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)))
                    .isEqualTo(8);
        });
        assertThat(attributes).isEmpty();
        parent.end();
    }

    @Test void optInCapturesContentButHideFlagsWin() {
        for (boolean hide : new boolean[]{false, true}) {
            FITracer configured = new FITracer(sdk.get("test"), TraceConfig.builder()
                    .hideInputs(hide).hideOutputMessages(hide).hideErrorDetails(hide).build());
            LangChain4jListener listener = new LangChain4jListener(configured, "synthetic", true);
            Map<Object, Object> attributes = new ConcurrentHashMap<>();
            listener.onRequest(new ChatModelRequestContext(request, attributes));
            listener.onResponse(new ChatModelResponseContext(response, request, attributes));
        }
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes().toString())
                .contains("private-input", "private-tool-args");
        assertThat(exporter.getFinishedSpanItems().get(1).getAttributes().toString()).doesNotContain("private-");
    }

    @Test void defaultErrorCaptureHasTypeButNeverErrorMessageOrStackTrace() {
        LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic");
        Map<Object, Object> attributes = new ConcurrentHashMap<>();
        listener.onRequest(new ChatModelRequestContext(request, attributes));
        listener.onError(new ChatModelErrorContext(new IllegalStateException("private-error"), request, null, attributes));
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEmpty();
            assertThat(span.getAttributes().toString()).doesNotContain("private-");
            assertThat(span.getEvents()).singleElement().satisfies(event -> {
                assertThat(event.getAttributes().asMap()).hasSize(1);
                assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.type")))
                        .isEqualTo(IllegalStateException.class.getName());
            });
        });
    }

    @Test void captureFailureDoesNotBreakTheCallAndLeavesSanitizedDiagnostics() {
        ChatModelRequest broken = new ChatModelRequest("model", null, null, null, null, null) {
            @Override public List<ChatMessage> messages() { throw new IllegalStateException("private-payload"); }
        };
        LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic", true);
        Map<Object, Object> attributes = new ConcurrentHashMap<>();
        listener.onRequest(new ChatModelRequestContext(broken, attributes));
        listener.onResponse(new ChatModelResponseContext(response, broken, attributes));
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getAttributes().get(AttributeKey.stringKey("traceai.instrumentation.error")))
                    .isEqualTo(IllegalStateException.class.getName());
            assertThat(span.getAttributes().toString()).doesNotContain("private-payload");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        });
    }

    @Test void legacyWrapperPlusListenerProducesOneModelSpan() {
        LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic");
        ChatLanguageModel delegate = messages -> {
            Map<Object, Object> attributes = new ConcurrentHashMap<>();
            listener.onRequest(new ChatModelRequestContext(request, attributes));
            listener.onResponse(new ChatModelResponseContext(response, request, attributes));
            return Response.from(AiMessage.from("answer"));
        };
        new TracedChatLanguageModel(delegate, tracer, "synthetic").generate(request.messages());
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test void concurrentRequestsKeepTheirOwnParentAndTerminalState() throws Exception {
        LangChain4jListener listener = new LangChain4jListener(tracer, "synthetic");
        Map<Object, Object> first = new ConcurrentHashMap<>();
        Map<Object, Object> second = new ConcurrentHashMap<>();
        Span parent1 = sdk.get("test").spanBuilder("first").startSpan();
        Span parent2 = sdk.get("test").spanBuilder("second").startSpan();
        try (Scope ignored = parent1.makeCurrent()) { listener.onRequest(new ChatModelRequestContext(request, first)); }
        try (Scope ignored = parent2.makeCurrent()) { listener.onRequest(new ChatModelRequestContext(request, second)); }
        Thread worker = new Thread(() -> {
            listener.onResponse(new ChatModelResponseContext(response, request, second));
            listener.onError(new ChatModelErrorContext(new RuntimeException("private-error"), request, null, first));
        });
        worker.start(); worker.join();
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        assertThat(exporter.getFinishedSpanItems().get(0).getParentSpanId()).isEqualTo(parent2.getSpanContext().getSpanId());
        assertThat(exporter.getFinishedSpanItems().get(1).getParentSpanId()).isEqualTo(parent1.getSpanContext().getSpanId());
        parent1.end(); parent2.end();
    }
}
