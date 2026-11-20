package ai.traceai.spring;

import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;

import java.util.List;
import java.util.Map;
import java.util.function.BiFunction;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** A Spring AI ToolCallback invocation is one TOOL span; arguments and results go through the masker. */
class TracedToolCallbackTest {
    private static final String SECRET_ARGS = "{\"city\":\"private-tool-arguments-DO-NOT-EXPORT\"}";
    private static final String SECRET_RESULT = "private-tool-result-DO-NOT-EXPORT";
    private static final String SECRET_DESCRIPTION = "private-tool-description-DO-NOT-EXPORT";

    private static final ToolDefinition DEFINITION = ToolDefinition.builder()
        .name("lookup").description(SECRET_DESCRIPTION).inputSchema("{\"type\":\"object\"}").build();

    /** A callback that supports both overloads, like the framework's method and function callbacks. */
    private static ToolCallback callback(BiFunction<String, ToolContext, String> body) {
        return new ToolCallback() {
            @Override
            public ToolDefinition getToolDefinition() {
                return DEFINITION;
            }

            @Override
            public String call(String toolInput) {
                return body.apply(toolInput, null);
            }

            @Override
            public String call(String toolInput, ToolContext toolContext) {
                return body.apply(toolInput, toolContext);
            }
        };
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

    /** Runs one call through the wrapper under the given config and returns the single exported span. */
    private static SpanData run(TraceConfig config, BiFunction<String, ToolContext, String> body, boolean withContext) {
        try (SpanTestSupport support = new SpanTestSupport(config)) {
            ToolCallback traced = new TracedToolCallback(callback(body), support.tracer);
            if (withContext) traced.call(SECRET_ARGS, new ToolContext(Map.of("user", "u1")));
            else traced.call(SECRET_ARGS);
            return support.single();
        }
    }

    @Test
    void singleArgumentCallIsOneInternalToolSpanNamedForTheTool() {
        SpanData data = run(TraceConfig.builder().build(), (in, ctx) -> SECRET_RESULT, false);

        assertThat(data.getName()).isEqualTo("execute_tool lookup");
        assertThat(data.getKind()).isEqualTo(SpanKind.INTERNAL);
        assertThat(attr(data, "fi.span.kind")).isEqualTo("TOOL");
        assertThat(attr(data, "gen_ai.operation.name")).isEqualTo("execute_tool");
        assertThat(attr(data, "gen_ai.tool.name")).isEqualTo("lookup");
        assertThat(attr(data, "gen_ai.tool.description")).isEqualTo(SECRET_DESCRIPTION);
        assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
        assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
        assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void callWithToolContextIsTracedAndPassesTheContextThrough() {
        ToolContext context = new ToolContext(Map.of("user", "u1"));
        ToolContext[] seen = new ToolContext[1];
        try (SpanTestSupport support = new SpanTestSupport()) {
            ToolCallback traced = new TracedToolCallback(callback((in, ctx) -> {
                seen[0] = ctx;
                return SECRET_RESULT;
            }), support.tracer);

            assertThat(traced.call(SECRET_ARGS, context)).isEqualTo(SECRET_RESULT);

            assertThat(seen[0]).isSameAs(context);
            SpanData data = support.single();
            assertThat(data.getName()).isEqualTo("execute_tool lookup");
            assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
            assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
        }
    }

    @Test
    void definitionAndMetadataAreTheDelegates() {
        ToolCallback delegate = callback((in, ctx) -> "ok");
        try (SpanTestSupport support = new SpanTestSupport()) {
            ToolCallback traced = new TracedToolCallback(delegate, support.tracer);
            assertThat(traced.getToolDefinition()).isSameAs(DEFINITION);
            assertThat(traced.getToolMetadata()).isEqualTo(delegate.getToolMetadata());
            assertThat(support.finished()).isEmpty();
        }
    }

    @Test
    void noCallIdIsRecordedBecauseSpringAiDoesNotProvideOne() {
        SpanData data = run(TraceConfig.builder().build(), (in, ctx) -> "ok", false);
        assertThat(data.getAttributes().get(AttributeKey.stringKey("gen_ai.tool.call.id"))).isNull();
    }

    @Test
    void spanIsAChildOfTheCurrentSpanAndIsCurrentWhileTheToolRuns() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            Span parent = support.provider.get("t").spanBuilder("agent").startSpan();
            String[] insideToolSpanId = new String[1];
            try (Scope ignored = parent.makeCurrent()) {
                new TracedToolCallback(callback((in, ctx) -> {
                    insideToolSpanId[0] = Span.current().getSpanContext().getSpanId();
                    return "ok";
                }), support.tracer).call("{}");
            } finally {
                parent.end();
            }
            SpanData tool = support.finished().stream().filter(s -> s.getName().startsWith("execute_tool")).findFirst().orElseThrow();
            assertThat(tool.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(insideToolSpanId[0]).isEqualTo(tool.getSpanId());
        }
    }

    @Test
    void failureRecordsTheExceptionSetsErrorAndRethrowsTheSameInstance() {
        IllegalStateException failure = new IllegalStateException("boom");
        try (SpanTestSupport support = new SpanTestSupport()) {
            ToolCallback traced = new TracedToolCallback(callback((in, ctx) -> {
                throw failure;
            }), support.tracer);

            assertThatThrownBy(() -> traced.call("{}")).isSameAs(failure);
            assertThatThrownBy(() -> traced.call("{}", new ToolContext(Map.of()))).isSameAs(failure);

            List<SpanData> spans = support.finished();
            assertThat(spans).hasSize(2);
            for (SpanData data : spans) {
                assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
                assertThat(data.getEvents()).anySatisfy(e -> assertThat(e.getName()).isEqualTo("exception"));
            }
        }
    }

    @Test
    void hideErrorDetailsKeepsTheExceptionMessageOut() {
        try (SpanTestSupport support = new SpanTestSupport(TraceConfig.builder().hideErrorDetails(true).build())) {
            ToolCallback traced = new TracedToolCallback(callback((in, ctx) -> {
                throw new IllegalStateException("private-error-text-DO-NOT-EXPORT");
            }), support.tracer);
            assertThatThrownBy(() -> traced.call("{}")).hasMessage("private-error-text-DO-NOT-EXPORT");
            SpanData data = support.single();
            assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(exports(data, "private-error-text")).isFalse();
        }
    }

    @Test
    void hideInputsKeepsArgumentsAndDescriptionOutButKeepsTheResult() {
        for (boolean withContext : new boolean[]{false, true}) {
            SpanData data = run(TraceConfig.builder().hideInputs(true).build(), (in, ctx) -> SECRET_RESULT, withContext);
            assertThat(exports(data, "private-tool-arguments")).isFalse();
            assertThat(exports(data, "private-tool-description")).isFalse();
            assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
            assertThat(attr(data, "gen_ai.tool.name")).isEqualTo("lookup");
        }
    }

    @Test
    void hideOutputsKeepsTheResultOutButKeepsTheArguments() {
        for (boolean withContext : new boolean[]{false, true}) {
            SpanData data = run(TraceConfig.builder().hideOutputs(true).build(), (in, ctx) -> SECRET_RESULT, withContext);
            assertThat(exports(data, "private-tool-result")).isFalse();
            assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
        }
    }

    @Test
    void hideInputMessagesKeepsArgumentsOut() {
        SpanData data = run(TraceConfig.builder().hideInputMessages(true).build(), (in, ctx) -> SECRET_RESULT, false);
        assertThat(exports(data, "private-tool-arguments")).isFalse();
        assertThat(attr(data, "output.value")).isEqualTo(SECRET_RESULT);
    }

    @Test
    void hideOutputMessagesKeepsTheResultOut() {
        SpanData data = run(TraceConfig.builder().hideOutputMessages(true).build(), (in, ctx) -> SECRET_RESULT, false);
        assertThat(exports(data, "private-tool-result")).isFalse();
        assertThat(attr(data, "input.value")).isEqualTo(SECRET_ARGS);
    }

    @Test
    void wrapAllWrapsEveryCallback() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            List<ToolCallback> wrapped = TracedToolCallback.wrapAll(
                List.of(callback((in, ctx) -> "a"), callback((in, ctx) -> "b")), support.tracer);
            assertThat(wrapped).hasSize(2).allMatch(c -> c instanceof TracedToolCallback);
            wrapped.get(1).call("{}");
            assertThat(support.single().getName()).isEqualTo("execute_tool lookup");
        }
    }
}
