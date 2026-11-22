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
        private final String toolName;
        StubLlm() { this("weather"); }
        StubLlm(String toolName) { super("gemini-2.0-flash"); this.toolName = toolName; }

        @Override
        public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
            boolean answered = request.contents().stream().anyMatch(c -> c.parts().orElse(List.of()).stream().anyMatch(p -> p.functionResponse().isPresent()));
            Content out = answered
                ? Content.builder().role("model").parts(Part.fromText(ANSWER)).build()
                : Content.builder().role("model").parts(Part.builder().functionCall(FunctionCall.builder().name(toolName).args(Map.of("city", "Kyoto")).id("call-1").build()).build()).build();
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

    /** A tool in the style of ADK's own LoadArtifactsTool: it overrides processLlmRequest and calls super. */
    static final class SuperCallingTool extends com.google.adk.tools.BaseTool {
        SuperCallingTool() { super("super_tool", "calls super"); }

        @Override
        public java.util.Optional<com.google.genai.types.FunctionDeclaration> declaration() {
            return java.util.Optional.of(com.google.genai.types.FunctionDeclaration.builder().name("super_tool").description("calls super").build());
        }

        @Override
        public io.reactivex.rxjava3.core.Completable processLlmRequest(LlmRequest.Builder builder, com.google.adk.tools.ToolContext context) {
            return super.processLlmRequest(builder, context);
        }

        @Override
        public io.reactivex.rxjava3.core.Single<Map<String, Object>> runAsync(Map<String, Object> args, com.google.adk.tools.ToolContext context) {
            return io.reactivex.rxjava3.core.Single.just(Map.of("ok", "SUPER-RESULT"));
        }
    }

    @Test
    void aWrappedToolThatCallsSuperIsStillTheOneAdkRunsAndTraces() throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), TraceConfig.builder().build());
            LlmAgent agent = LlmAgent.builder().name("super_agent").description("uses a tool that calls super")
                .model(new TracedLlm(new StubLlm("super_tool"), tracer)).instruction("Be brief")
                .tools(new TracedTool(new SuperCallingTool(), tracer)).build();
            InMemoryRunner runner = new InMemoryRunner(agent);
            var session = runner.sessionService().createSession(runner.appName(), "user-1").blockingGet();
            runner.runAsync("user-1", session.id(), Content.fromParts(Part.fromText(PROMPT))).toList().blockingGet();
            List<SpanData> tool = exporter.getFinishedSpanItems().stream().filter(sp -> sp.getName().startsWith("execute_tool")).toList();
            assertThat(tool).as("the TOOL span is recorded").hasSize(1);
            assertThat(attr(tool.get(0), "gen_ai.tool.call.result")).contains("SUPER-RESULT");
        } finally {
            provider.close();
        }
    }

    private static final class ResultTool extends com.google.adk.tools.BaseTool {
        private final Map<String, Object> result;
        ResultTool(Map<String, Object> result) { super("result_tool", "returns what it is given"); this.result = result; }

        @Override
        public io.reactivex.rxjava3.core.Single<Map<String, Object>> runAsync(Map<String, Object> args, com.google.adk.tools.ToolContext context) {
            return io.reactivex.rxjava3.core.Single.just(result);
        }
    }

    private static SpanData toolSpanFor(Map<String, Object> result) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), TraceConfig.builder().build());
            Map<String, Object> seen = new TracedTool(new ResultTool(result), tracer).runAsync(Map.of("a", "b"), null).blockingGet();
            assertThat(seen).as("the application sees the tool's real result").isSameAs(result);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            return exporter.getFinishedSpanItems().get(0);
        } finally {
            provider.close();
        }
    }

    @Test
    void aToolResultGsonCannotSerializeNeverFailsTheCall() {
        SpanData span = toolSpanFor(Map.of("when", java.time.Instant.parse("2026-01-02T03:04:05Z")));
        assertThat(attr(span, "gen_ai.tool.call.result")).contains("<unserializable: java.time.Instant>").doesNotContain("2026");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.OK);
    }

    @Test
    void aCyclicToolResultNeverFailsTheCall() {
        Map<String, Object> cyclic = new java.util.HashMap<>();
        cyclic.put("self", cyclic);
        SpanData span = toolSpanFor(cyclic);
        assertThat(attr(span, "gen_ai.tool.call.result")).contains("<cycle>");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.OK);
    }

    private static SpanData failedModelCall(TraceConfig config) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), config);
            BaseLlm failing = new BaseLlm("gemini-2.0-flash") {
                @Override
                public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
                    return Flowable.just(LlmResponse.builder().errorMessage("SEED-MODEL-ERROR-TEXT").build());
                }

                @Override
                public BaseLlmConnection connect(LlmRequest request) { throw new UnsupportedOperationException(); }
            };
            LlmRequest request = LlmRequest.builder().model("gemini-2.0-flash")
                .contents(List.of(Content.fromParts(Part.fromText(PROMPT)))).build();
            new TracedLlm(failing, tracer).generateContent(request, false).blockingLast();
            return exporter.getFinishedSpanItems().get(0);
        } finally {
            provider.close();
        }
    }

    @Test
    void aModelErrorMessageIsTheStatusDescriptionOnlyWhileErrorDetailsAreShown() {
        SpanData shown = failedModelCall(TraceConfig.builder().build());
        assertThat(shown.getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
        assertThat(shown.getStatus().getDescription()).isEqualTo("SEED-MODEL-ERROR-TEXT");
        SpanData hidden = failedModelCall(TraceConfig.builder().hideErrorDetails(true).build());
        assertThat(hidden.getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
        assertThat(hidden.getStatus().getDescription()).isEmpty();
        assertThat(hidden.getAttributes().toString()).doesNotContain("SEED-MODEL-ERROR-TEXT");
    }

    /** A model call that fails: its error text and the output it carries both hold the seed. */
    private static SpanData failedModelCallWithOutput(TraceConfig config) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), config);
            BaseLlm failing = new BaseLlm("gemini-2.0-flash") {
                @Override
                public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
                    return Flowable.just(LlmResponse.builder().errorMessage("SEED-MODEL-ERROR-TEXT")
                        .content(Content.builder().role("model").parts(Part.fromText("SEED-FAILED-OUTPUT")).build()).build());
                }

                @Override
                public BaseLlmConnection connect(LlmRequest request) { throw new UnsupportedOperationException(); }
            };
            LlmRequest request = LlmRequest.builder().model("gemini-2.0-flash")
                .contents(List.of(Content.fromParts(Part.fromText(PROMPT)))).build();
            new TracedLlm(failing, tracer).generateContent(request, false).blockingLast();
            return exporter.getFinishedSpanItems().get(0);
        } finally {
            provider.close();
        }
    }

    private static boolean hasOutputAttribute(SpanData span) {
        return span.getAttributes().asMap().keySet().stream()
            .anyMatch(k -> k.getKey().equals("output.value") || k.getKey().equals("output.mime_type") || k.getKey().startsWith("gen_ai.output."));
    }

    @Test
    void aFailedModelCallCarriesItsOutputOnlyWhileErrorDetailsAreShown() {
        SpanData shown = failedModelCallWithOutput(TraceConfig.builder().build());
        assertThat(attr(shown, "gen_ai.output.messages.0.message.content")).isEqualTo("SEED-FAILED-OUTPUT");
        SpanData hidden = failedModelCallWithOutput(TraceConfig.builder().hideErrorDetails(true).build());
        assertThat(hidden.getStatus().getStatusCode()).isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
        assertThat(hasOutputAttribute(hidden)).as(hidden.getAttributes().toString()).isFalse();
        assertThat(hidden.getAttributes().toString()).doesNotContain("SEED-FAILED-OUTPUT").doesNotContain("SEED-MODEL-ERROR-TEXT");
    }

    @Test
    void aSuccessfulModelCallKeepsItsOutputWhileErrorDetailsAreHidden() throws Exception {
        List<SpanData> spans = run(TraceConfig.builder().hideErrorDetails(true).build());
        SpanData second = spans.stream().filter(s -> "LLM".equals(attr(s, "gen_ai.span.kind"))).toList().get(1);
        assertThat(attr(second, "gen_ai.output.messages.0.message.content")).isEqualTo(ANSWER);
    }

    /** A tool call that fails the way ADK's FunctionTool reports it: a result with an "error" entry. */
    private static SpanData toolSpanForResult(TraceConfig config, Map<String, Object> result) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            FITracer tracer = new FITracer(provider.get("adk-test"), config);
            var tool = new com.google.adk.tools.BaseTool("boom", "returns " + "a result") {
                @Override
                public io.reactivex.rxjava3.core.Single<Map<String, Object>> runAsync(Map<String, Object> args, com.google.adk.tools.ToolContext ctx) {
                    return io.reactivex.rxjava3.core.Single.just(result);
                }
            };
            new TracedTool(tool, tracer).runAsync(Map.of("a", "b"), null).blockingGet();
            return exporter.getFinishedSpanItems().get(0);
        } finally {
            provider.close();
        }
    }

    @Test
    void aFailedToolResultIsRecordedOnlyWhileErrorDetailsAreShown() {
        Map<String, Object> failed = Map.of("status", "error", "error", "SEED-TOOL-ERROR-TEXT");
        assertThat(attr(toolSpanForResult(TraceConfig.builder().build(), failed), "gen_ai.tool.call.result")).contains("SEED-TOOL-ERROR-TEXT");
        SpanData hidden = toolSpanForResult(TraceConfig.builder().hideErrorDetails(true).build(), failed);
        assertThat(attr(hidden, "gen_ai.tool.call.result")).isNull();
        assertThat(hidden.getAttributes().toString()).doesNotContain("SEED-TOOL-ERROR-TEXT");
        assertThat(attr(hidden, "gen_ai.tool.name")).isEqualTo("boom");
    }

    @Test
    void aSuccessfulToolResultIsKeptWhileErrorDetailsAreHidden() {
        SpanData span = toolSpanForResult(TraceConfig.builder().hideErrorDetails(true).build(), Map.of("forecast", "SEED-OK-RESULT"));
        assertThat(attr(span, "gen_ai.tool.call.result")).contains("SEED-OK-RESULT");
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
