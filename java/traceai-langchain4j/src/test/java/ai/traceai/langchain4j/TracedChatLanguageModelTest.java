package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import dev.langchain4j.model.output.TokenUsage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.*;

class TracedChatLanguageModelTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final FITracer tracer = new FITracer(provider.get("test"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
    private final List<ChatMessage> messages = List.of(UserMessage.from("private prompt"));
    private final ToolSpecification tool = ToolSpecification.builder().name("lookup").build();

    @AfterEach void close() { provider.close(); }

    @Test void delegatesBothToolOverloadsWithoutChangingArgumentsOrResponses() {
        AtomicInteger calls = new AtomicInteger();
        Response<AiMessage> expected = Response.from(AiMessage.from("answer"));
        List<ToolSpecification> tools = List.of(tool);
        ChatLanguageModel delegate = new ChatLanguageModel() {
            @Override public Response<AiMessage> generate(List<ChatMessage> input) {
                throw new AssertionError("Wrong overload");
            }
            @Override public Response<AiMessage> generate(List<ChatMessage> input, List<ToolSpecification> specs) {
                assertThat(input).isSameAs(messages);
                assertThat(specs).isSameAs(tools);
                calls.incrementAndGet();
                return expected;
            }
            @Override public Response<AiMessage> generate(List<ChatMessage> input, ToolSpecification spec) {
                assertThat(input).isSameAs(messages);
                assertThat(spec).isSameAs(tool);
                calls.incrementAndGet();
                return expected;
            }
        };
        TracedChatLanguageModel wrapped = new TracedChatLanguageModel(delegate, tracer, "synthetic");
        assertThat(wrapped.generate(messages, tools)).isSameAs(expected);
        assertThat(wrapped.generate(messages, tool)).isSameAs(expected);
        assertThat(calls).hasValue(2);
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span ->
                assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK));
    }

    @Test void doesNotExportHiddenToolArgumentsOrToolNames() {
        ChatLanguageModel delegate = input -> Response.from(AiMessage.from(ToolExecutionRequest.builder()
                .id("private-id").name("private-name").arguments("private-arguments").build()));
        new TracedChatLanguageModel(delegate, tracer, "synthetic").generate(messages);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getAttributes().toString()).doesNotContain("private-");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_OUTPUT_MESSAGES)))
                    .isNull();
        });
    }

    @Test void nullableTokenCountsDoNotBreakAValidResponse() {
        Response<AiMessage> expected = Response.from(AiMessage.from("answer"), new TokenUsage(null, null));
        ChatLanguageModel delegate = input -> expected;
        assertThat(new TracedChatLanguageModel(delegate, tracer, "synthetic").generate(messages)).isSameAs(expected);
    }

    @Test void preservesTheOriginalToolErrorAndEndsTheSpan() {
        IllegalArgumentException expected = new IllegalArgumentException("private error");
        ChatLanguageModel delegate = new ChatLanguageModel() {
            @Override public Response<AiMessage> generate(List<ChatMessage> input) { throw new AssertionError(); }
            @Override public Response<AiMessage> generate(List<ChatMessage> input, List<ToolSpecification> tools) {
                throw expected;
            }
        };
        assertThatThrownBy(() -> new TracedChatLanguageModel(delegate, tracer, "synthetic")
                .generate(messages, List.of(tool))).isSameAs(expected);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().toString()).doesNotContain("private error");
        });
    }
}
