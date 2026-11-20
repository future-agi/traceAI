package ai.traceai.googleadk;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.adk.agents.LlmAgent;
import com.google.adk.events.Event;
import com.google.adk.models.BaseLlm;
import com.google.adk.models.BaseLlmConnection;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.adk.tools.Annotations.Schema;
import com.google.adk.tools.FunctionTool;
import com.google.genai.types.Content;
import com.google.genai.types.FunctionCall;
import com.google.genai.types.GenerateContentResponseUsageMetadata;
import com.google.genai.types.Part;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Flowable;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** A real ADK agent with a stub model, run through the wrappers: no provider calls and no keys. */
public class TracedAdkTest {
    private static final String PROMPT = "SENTINEL-PROMPT-weather-in-Kyoto";
    private static final String ANSWER = "SENTINEL-ANSWER-sunny";

    public static Map<String, Object> weather(@Schema(name = "city") String city) {
        return Map.of("city", city, "forecast", "SENTINEL-TOOL-RESULT");
    }

    static final class StubLlm extends BaseLlm {
        StubLlm() { super("gemini-2.0-flash"); }

        @Override
        public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
            boolean answered = request.contents().stream().anyMatch(c -> c.parts().orElse(List.of()).stream().anyMatch(p -> p.functionResponse().isPresent()));
            Content out = answered
                ? Content.builder().role("model").parts(Part.fromText(ANSWER)).build()
                : Content.builder().role("model").parts(Part.builder().functionCall(FunctionCall.builder().name("weather").args(Map.of("city", "Kyoto")).id("call-1").build()).build()).build();
            return Flowable.just(LlmResponse.builder().content(out)
                .usageMetadata(GenerateContentResponseUsageMetadata.builder().promptTokenCount(11).candidatesTokenCount(7).totalTokenCount(18).build()).build());
        }

        @Override
        public BaseLlmConnection connect(LlmRequest request) { throw new UnsupportedOperationException(); }
    }

    /** A built-in-style tool: no function declaration; it changes the model request itself, as Google Search and code execution do. */
    static final class RequestShapingTool extends com.google.adk.tools.BaseTool {
        RequestShapingTool() { super("built_in", "changes the request"); }

        @Override
        public io.reactivex.rxjava3.core.Completable processLlmRequest(LlmRequest.Builder builder, com.google.adk.tools.ToolContext context) {
            builder.model("model-chosen-by-the-built-in-tool");
            return io.reactivex.rxjava3.core.Completable.complete();
        }
    }

    @Test
    void aWrappedBuiltInToolStillShapesTheModelRequest() {
        FITracer tracer = new FITracer(SdkTracerProvider.builder().build().get("adk-test"), TraceConfig.builder().build());
        LlmRequest.Builder bare = LlmRequest.builder();
        new RequestShapingTool().processLlmRequest(bare, null).blockingAwait();
        assertThat(bare.build().model()).hasValue("model-chosen-by-the-built-in-tool");

        LlmRequest.Builder wrapped = LlmRequest.builder();
        new TracedTool(new RequestShapingTool(), tracer).processLlmRequest(wrapped, null).blockingAwait();
        assertThat(wrapped.build().model()).hasValue("model-chosen-by-the-built-in-tool");
    }

    @Test
    void aWrappedFunctionToolIsStillAddedToTheRequestAsBefore() throws Exception {
        FITracer tracer = new FITracer(SdkTracerProvider.builder().build().get("adk-test"), TraceConfig.builder().build());
        FunctionTool function = FunctionTool.create(TracedAdkTest.class, "weather");
        LlmRequest.Builder plain = LlmRequest.builder();
        function.processLlmRequest(plain, null).blockingAwait();
        LlmRequest.Builder wrapped = LlmRequest.builder();
        new TracedTool(function, tracer).processLlmRequest(wrapped, null).blockingAwait();
        assertThat(wrapped.build().config().toString()).isEqualTo(plain.build().config().toString());
    }

    private static List<SpanData> run(TraceConfig config) throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), config);
            LlmAgent agent = LlmAgent.builder().name("weather_agent").description("answers weather questions")
                .model(new TracedLlm(new StubLlm(), tracer)).instruction("Be brief")
                .tools(new TracedTool(FunctionTool.create(TracedAdkTest.class, "weather"), tracer)).build();
            InMemoryRunner runner = new InMemoryRunner(agent);
            var session = runner.sessionService().createSession(runner.appName(), "user-1").blockingGet();
            List<Event> events = runner.runAsync("user-1", session.id(), Content.fromParts(Part.fromText(PROMPT))).toList().blockingGet();
            assertThat(events).hasSize(3);
            return exporter.getFinishedSpanItems();
        } finally {
            provider.close();
        }
    }

    private static String attr(SpanData span, String key) {
        Object v = span.getAttributes().get(AttributeKey.stringKey(key));
        return v == null ? null : String.valueOf(v);
    }

    @Test
    void llmAndToolCallsAreTraced() throws Exception {
        List<SpanData> spans = run(TraceConfig.builder().build());
        List<SpanData> llm = spans.stream().filter(s -> "LLM".equals(attr(s, "gen_ai.span.kind"))).toList();
        List<SpanData> tool = spans.stream().filter(s -> "TOOL".equals(attr(s, "gen_ai.span.kind"))).toList();
        assertThat(llm).hasSize(2);
        assertThat(tool).hasSize(1);
        SpanData second = llm.get(1);
        assertThat(attr(second, "gen_ai.request.model")).isEqualTo("gemini-2.0-flash");
        assertThat(attr(second, "gen_ai.input.messages.0.message.role")).isEqualTo("system");
        assertThat(attr(second, "gen_ai.input.messages.1.message.content")).isEqualTo(PROMPT);
        assertThat(attr(second, "gen_ai.input.messages.2.message.tool_calls.0.tool_call.function.name")).isEqualTo("weather");
        assertThat(attr(second, "gen_ai.input.messages.3.message.role")).isEqualTo("tool");
        assertThat(attr(second, "gen_ai.output.messages.0.message.content")).isEqualTo(ANSWER);
        assertThat(second.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens"))).isEqualTo(11L);
        assertThat(attr(llm.get(0), "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name")).isEqualTo("weather");
        assertThat(attr(tool.get(0), "gen_ai.tool.name")).isEqualTo("weather");
        assertThat(attr(tool.get(0), "gen_ai.tool.call.arguments")).contains("Kyoto");
        assertThat(attr(tool.get(0), "gen_ai.tool.call.result")).contains("SENTINEL-TOOL-RESULT");
    }

    @Test
    void hideInputsAndOutputsKeepContentOffTheSpans() throws Exception {
        List<SpanData> spans = run(TraceConfig.builder().hideInputs(true).hideOutputs(true).build());
        assertThat(spans).isNotEmpty();
        for (SpanData span : spans) {
            String all = span.getAttributes().toString();
            assertThat(all).doesNotContain(PROMPT).doesNotContain(ANSWER).doesNotContain("SENTINEL-TOOL-RESULT");
        }
        assertThat(spans.stream().filter(s -> "LLM".equals(attr(s, "gen_ai.span.kind"))).count()).isEqualTo(2);
    }

    @Test
    void aFailingToolIsAnErrorSpan() throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), TraceConfig.builder().build());
            var failing = new com.google.adk.tools.BaseTool("boom", "always fails") {
                @Override
                public io.reactivex.rxjava3.core.Single<Map<String, Object>> runAsync(Map<String, Object> args, com.google.adk.tools.ToolContext ctx) {
                    return io.reactivex.rxjava3.core.Single.error(new IllegalStateException("tool failed"));
                }
            };
            new TracedTool(failing, tracer).runAsync(Map.of("a", "b"), null).test().assertError(IllegalStateException.class);
            List<SpanData> spans = exporter.getFinishedSpanItems();
            assertThat(spans).hasSize(1);
            assertThat(spans.get(0).getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
            assertThat(attr(spans.get(0), "gen_ai.tool.name")).isEqualTo("boom");
        } finally {
            provider.close();
        }
    }
}
