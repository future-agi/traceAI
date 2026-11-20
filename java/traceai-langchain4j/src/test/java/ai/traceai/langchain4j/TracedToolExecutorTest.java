package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.ToolExecutionResultMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import dev.langchain4j.service.AiServices;
import dev.langchain4j.service.tool.ToolExecutor;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** An AiServices tool execution is one TOOL span; arguments and results go through the masker. */
class TracedToolExecutorTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String SECRET_ARGS = "{\"city\":\"private-tool-arguments-DO-NOT-EXPORT\"}";
    private static final String SECRET_RESULT = "private-tool-result-DO-NOT-EXPORT";
    private static final String SECRET_DESCRIPTION = "private-tool-description-DO-NOT-EXPORT";

    private static final ToolSpecification SPEC = ToolSpecification.builder()
        .name("lookup").description(SECRET_DESCRIPTION).build();
    private static final ToolExecutionRequest REQUEST = ToolExecutionRequest.builder()
        .id("call-7").name("lookup").arguments(SECRET_ARGS).build();

    @BeforeEach
    void reset() {
        otel.clearSpans();
    }

    private static FITracer tracer(TraceConfig config) {
        return new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
    }

    private static ToolExecutor traced(TraceConfig config, ToolExecutor delegate) {
        return new TracedToolExecutor(delegate, SPEC, tracer(config));
    }

    private static SpanData only() {
        List<SpanData> spans = otel.getSpans();
        assertThat(spans).hasSize(1);
        return spans.get(0);
    }

    private static String attr(SpanData data, String key) {
        return data.getAttributes().get(AttributeKey.stringKey(key));
    }

    private static boolean exports(SpanData data, String needle) {
        if (data.getAttributes().asMap().values().stream().anyMatch(v -> String.valueOf(v).contains(needle))) return true;
        if (data.getName().contains(needle) || String.valueOf(data.getStatus().getDescription()).contains(needle)) return true;
        for (EventData event : data.getEvents()) {
            if (event.getAttributes().asMap().values().stream().anyMatch(v -> String.valueOf(v).contains(needle))) return true;
        }
        return false;
    }

    @Test
    void successIsOneInternalToolSpanNamedForTheTool() {
        String result = traced(TraceConfig.builder().build(), (request, memoryId) -> SECRET_RESULT).execute(REQUEST, "m1");

        assertThat(result).isEqualTo(SECRET_RESULT);
        SpanData data = only();
        assertThat(data.getName()).isEqualTo("execute_tool lookup");
        assertThat(data.getKind()).isEqualTo(SpanKind.INTERNAL);
        assertThat(attr(data, "fi.span.kind")).isEqualTo("TOOL");
        assertThat(attr(data, "gen_ai.operation.name")).isEqualTo("execute_tool");
        assertThat(attr(data, "gen_ai.tool.name")).isEqualTo("lookup");
        assertThat(attr(data, "gen_ai.tool.call.id")).isEqualTo("call-7");
        assertThat(attr(data, "gen_ai.tool.description")).isEqualTo(SECRET_DESCRIPTION);
        assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
        assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
        assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void theExecutorReceivesTheOriginalRequestAndMemoryId() {
        Object[] seen = new Object[2];
        traced(TraceConfig.builder().build(), (request, memoryId) -> {
            seen[0] = request;
            seen[1] = memoryId;
            return "ok";
        }).execute(REQUEST, "memory-3");
        assertThat(seen[0]).isSameAs(REQUEST);
        assertThat(seen[1]).isEqualTo("memory-3");
    }

    @Test
    void spanIsAChildOfTheCurrentSpanAndIsCurrentWhileTheToolRuns() {
        FITracer tracer = tracer(TraceConfig.builder().build());
        Span parent = otel.getOpenTelemetry().getTracer("t").spanBuilder("agent").startSpan();
        String[] insideToolSpanId = new String[1];
        try (Scope ignored = parent.makeCurrent()) {
            new TracedToolExecutor((request, memoryId) -> {
                insideToolSpanId[0] = Span.current().getSpanContext().getSpanId();
                return "ok";
            }, SPEC, tracer).execute(REQUEST, null);
        } finally {
            parent.end();
        }
        SpanData tool = otel.getSpans().stream().filter(s -> s.getName().startsWith("execute_tool")).findFirst().orElseThrow();
        assertThat(tool.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        assertThat(tool.getTraceId()).isEqualTo(parent.getSpanContext().getTraceId());
        assertThat(insideToolSpanId[0]).isEqualTo(tool.getSpanId());
    }

    @Test
    void failureRecordsTheExceptionSetsErrorAndRethrowsTheSameInstance() {
        IllegalStateException failure = new IllegalStateException("boom");
        assertThatThrownBy(() -> traced(TraceConfig.builder().build(), (request, memoryId) -> {
            throw failure;
        }).execute(REQUEST, null)).isSameAs(failure);

        SpanData data = only();
        assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(data.getEvents()).anySatisfy(e -> {
            assertThat(e.getName()).isEqualTo("exception");
            assertThat(e.getAttributes().get(AttributeKey.stringKey("exception.type"))).isEqualTo(IllegalStateException.class.getName());
        });
    }

    @Test
    void anErrorIsRethrownUnchangedToo() {
        AssertionError failure = new AssertionError("fatal");
        assertThatThrownBy(() -> traced(TraceConfig.builder().build(), (request, memoryId) -> {
            throw failure;
        }).execute(REQUEST, null)).isSameAs(failure);
        assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void hideErrorDetailsKeepsTheExceptionMessageOut() {
        assertThatThrownBy(() -> traced(TraceConfig.builder().hideErrorDetails(true).build(), (request, memoryId) -> {
            throw new IllegalStateException("private-error-text-DO-NOT-EXPORT");
        }).execute(REQUEST, null)).hasMessage("private-error-text-DO-NOT-EXPORT");
        SpanData data = only();
        assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(exports(data, "private-error-text")).isFalse();
    }

    @Test
    void hideInputsKeepsArgumentsAndDescriptionOutButKeepsTheResult() {
        traced(TraceConfig.builder().hideInputs(true).build(), (request, memoryId) -> SECRET_RESULT).execute(REQUEST, null);
        SpanData data = only();
        assertThat(exports(data, "private-tool-arguments")).isFalse();
        assertThat(exports(data, "private-tool-description")).isFalse();
        assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
        assertThat(attr(data, "gen_ai.tool.name")).isEqualTo("lookup");
        assertThat(attr(data, "gen_ai.tool.call.id")).isEqualTo("call-7");
    }

    @Test
    void hideOutputsKeepsTheResultOutButKeepsTheArguments() {
        traced(TraceConfig.builder().hideOutputs(true).build(), (request, memoryId) -> SECRET_RESULT).execute(REQUEST, null);
        SpanData data = only();
        assertThat(exports(data, "private-tool-result")).isFalse();
        assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
    }

    @Test
    void hideInputMessagesKeepsArgumentsOut() {
        traced(TraceConfig.builder().hideInputMessages(true).build(), (request, memoryId) -> SECRET_RESULT).execute(REQUEST, null);
        SpanData data = only();
        assertThat(exports(data, "private-tool-arguments")).isFalse();
        assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
    }

    @Test
    void hideOutputMessagesKeepsTheResultOut() {
        traced(TraceConfig.builder().hideOutputMessages(true).build(), (request, memoryId) -> SECRET_RESULT).execute(REQUEST, null);
        SpanData data = only();
        assertThat(exports(data, "private-tool-result")).isFalse();
        assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
    }

    @Test
    void typedContentKeysAreMaskedByKeyUnderTheHideSettings() {
        traced(TraceConfig.builder().hideInputs(true).hideOutputs(true).build(), (request, memoryId) -> SECRET_RESULT).execute(REQUEST, null);
        SpanData data = only();
        for (String needle : List.of("private-tool-arguments", "private-tool-result", "private-tool-description")) {
            assertThat(exports(data, needle)).as(needle).isFalse();
        }
    }

    @Test
    void descriptionAndCallIdAreOmittedWhenTheFrameworkDoesNotProvideThem() {
        ToolExecutionRequest anonymous = ToolExecutionRequest.builder().name("lookup").arguments("{}").build();
        new TracedToolExecutor((request, memoryId) -> "ok", tracer(TraceConfig.builder().build())).execute(anonymous, null);
        SpanData data = only();
        assertThat(data.getAttributes().get(AttributeKey.stringKey("gen_ai.tool.description"))).isNull();
        assertThat(data.getAttributes().get(AttributeKey.stringKey("gen_ai.tool.call.id"))).isNull();
        assertThat(attr(data, "gen_ai.tool.name")).isEqualTo("lookup");
    }

    @Test
    void wrapAllKeepsEverySpecificationAndTracesEachExecutor() {
        ToolSpecification other = ToolSpecification.builder().name("other").description("d").build();
        Map<ToolSpecification, ToolExecutor> wrapped = TracedToolExecutor.wrapAll(
            Map.of(SPEC, (request, memoryId) -> "a", other, (request, memoryId) -> "b"), tracer(TraceConfig.builder().build()));

        assertThat(wrapped).containsOnlyKeys(SPEC, other);
        wrapped.get(other).execute(ToolExecutionRequest.builder().name("other").arguments("{}").build(), null);
        assertThat(only().getName()).isEqualTo("execute_tool other");
        assertThat(attr(only(), "gen_ai.tool.description")).isEqualTo("d");
    }

    /** Annotated tool object, as AiServices.tools(Object...) takes it. */
    static final class Weather {
        @Tool("Current weather for a city")
        public String weather(@P("city name") String city) {
            return "sunny in " + city;
        }
    }

    @Test
    void forToolsTracesAnnotatedToolMethodsWithTheirDescription() {
        Map<ToolSpecification, ToolExecutor> tools = TracedToolExecutor.forTools(tracer(TraceConfig.builder().build()), new Weather());
        assertThat(tools).hasSize(1);
        Map.Entry<ToolSpecification, ToolExecutor> entry = tools.entrySet().iterator().next();

        String result = entry.getValue().execute(ToolExecutionRequest.builder()
            .id("c1").name("weather").arguments("{\"arg0\":\"Lisbon\"}").build(), null);

        assertThat(result).isEqualTo("sunny in Lisbon");
        SpanData data = only();
        assertThat(data.getName()).isEqualTo("execute_tool weather");
        assertThat(attr(data, "gen_ai.tool.description")).isEqualTo("Current weather for a city");
        assertThat(attr(data, "output.value")).isEqualTo("sunny in Lisbon");
    }

    interface Assistant {
        String chat(String message);
    }

    /** First turn asks for the tool, the second answers with the tool result. */
    static final class ScriptedModel implements ChatLanguageModel {
        @Override
        public Response<AiMessage> generate(List<ChatMessage> messages) {
            return generate(messages, List.of());
        }

        @Override
        public Response<AiMessage> generate(List<ChatMessage> messages, List<ToolSpecification> specifications) {
            ChatMessage last = messages.get(messages.size() - 1);
            if (last instanceof ToolExecutionResultMessage result) {
                return Response.from(AiMessage.from("The answer is " + result.text()));
            }
            return Response.from(AiMessage.from(ToolExecutionRequest.builder()
                .id("call-ai").name("weather").arguments("{\"arg0\":\"Porto\"}").build()));
        }
    }

    @Test
    void anAiServicesToolRoundTripProducesOneToolSpan() {
        Assistant assistant = AiServices.builder(Assistant.class)
            .chatLanguageModel(new ScriptedModel())
            .tools(TracedToolExecutor.forTools(tracer(TraceConfig.builder().build()), new Weather()))
            .build();

        assertThat(assistant.chat("weather in Porto?")).isEqualTo("The answer is sunny in Porto");

        SpanData data = only();
        assertThat(data.getName()).isEqualTo("execute_tool weather");
        assertThat(attr(data, "gen_ai.tool.call.id")).isEqualTo("call-ai");
        assertThat(attr(data, "input.value")).isEqualTo("{\"arg0\":\"Porto\"}");
        assertThat(attr(data, "output.value")).isEqualTo("sunny in Porto");
    }
}
