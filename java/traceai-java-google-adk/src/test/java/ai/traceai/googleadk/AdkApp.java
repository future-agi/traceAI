package ai.traceai.googleadk;

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
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Flowable;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

/**
 * Child process for the javaagent module's GoogleAdkE2ETest: a real ADK for Java agent with a tool, whose model stub calls a local
 * OpenAI-compatible endpoint through the JDK HttpClient while ADK's model call is subscribed, as a real model
 * implementation does. No tracing code, no provider calls.
 *
 * <p>Arguments: mode ("plain" or "sdk", the application installs its own OpenTelemetry SDK), the model endpoint, the prompt, and
 * optionally where the model's HTTP call is sent ("subscribe", "assembly" or "executor").
 */
public class AdkApp {
    static volatile String endpoint;
    /** The tool also makes an LLM HTTP call of its own (scenario "tool-call"). */
    static volatile boolean toolCallsModel;
    /** Scenario "error": the model answers with an error message and some output. */
    static volatile boolean failingModel;
    /** The name of the function the model asks for: the plain tool, or (scenario "annotated-tool") the one that carries a TraceAI annotation. */
    static volatile String toolName = "weather";
    /** Scenario "tool-error": the tool reports a failure in its result, and the model answers after it. */
    static volatile boolean failingTool;
    /** Scenarios "wrapped", "wrapped-llm" and "wrapped-tool": the model and/or the tool go through TracedLlm / TracedTool, whose spans a provider of the wrappers' own records. */
    static InMemorySpanExporter wrapperSpans;
    static final java.util.concurrent.CountDownLatch ENTERED = new java.util.concurrent.CountDownLatch(1);
    static final java.util.concurrent.CountDownLatch RELEASE = new java.util.concurrent.CountDownLatch(1);

    static void post() throws Exception { post("hi"); }

    static void post(String content) throws Exception { post(content, "", null); }

    /** An LLM HTTP call whose body, query and a header can each carry {@code marker}. */
    static void post(String content, String query, String header) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(endpoint + "/v1/chat/completions" + query)).header("content-type", "application/json");
        if (header != null) builder.header("x-spanless-marker", header);
        HttpRequest http = builder
            .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"" + content + "\"}]}")).build();
        HttpClient.newHttpClient().send(http, HttpResponse.BodyHandlers.ofString());
    }

    public static Map<String, Object> weather(@Schema(name = "city") String city) throws Exception {
        if (toolCallsModel) post();
        return Map.of("city", city, "forecast", "SENTINEL-TOOL-RESULT");
    }

    /** The tool of scenario "annotated-tool": an annotated application method that ADK calls, which makes its own HTTP request. */
    @ai.traceai.annotations.Tool(name = "weather_lookup")
    public static Map<String, Object> annotatedWeather(@Schema(name = "city") String city) throws Exception {
        post();
        return Map.of("city", city, "forecast", "SENTINEL-TOOL-RESULT");
    }

    /** The weather tool as a tool of its own that reports a failure in its result, with the seed in the message. */
    static final class FailingTool extends com.google.adk.tools.BaseTool {
        private final FunctionTool function = FunctionTool.create(AdkApp.class, "weather");
        FailingTool() { super("weather", "answers with an error"); }

        @Override
        public java.util.Optional<com.google.genai.types.FunctionDeclaration> declaration() { return function.declaration(); }

        @Override
        public io.reactivex.rxjava3.core.Single<Map<String, Object>> runAsync(Map<String, Object> args, com.google.adk.tools.ToolContext context) {
            // The shape ADK's own FunctionTool answers with when its function throws, with the seed as the message.
            return io.reactivex.rxjava3.core.Single.just(Map.of("status", "error", "message", "SEED-TOOL-ERROR"));
        }
    }

    /** The weather tool whose work is deferred: it makes its HTTP call when the result is subscribed to, not when the call is made. */
    static final class DeferredTool extends com.google.adk.tools.BaseTool {
        private final FunctionTool function = FunctionTool.create(AdkApp.class, "weather");
        DeferredTool() { super("weather", "answers lazily"); }

        @Override
        public java.util.Optional<com.google.genai.types.FunctionDeclaration> declaration() { return function.declaration(); }

        @Override
        public io.reactivex.rxjava3.core.Single<Map<String, Object>> runAsync(Map<String, Object> args, com.google.adk.tools.ToolContext context) {
            return io.reactivex.rxjava3.core.Single.fromCallable(() -> { post(); return Map.of("city", "Kyoto", "forecast", "SENTINEL-TOOL-RESULT"); });
        }
    }

    static final class Model extends BaseLlm {
        /** Where the model's HTTP call is sent: "subscribe" (while subscribed), "assembly" (inside generateContent, before the Flowable
         *  exists) or "executor" (on a thread of the client's own executor, which carries no OpenTelemetry context). */
        private final String send;
        /** The tool the model asks for first, and whether it only ever answers in text (a sub-agent's model). */
        private final String callName;
        private final boolean answerOnly;
        private final java.util.concurrent.ExecutorService executor = java.util.concurrent.Executors.newCachedThreadPool(r -> { Thread t = new Thread(r, "model-client"); t.setDaemon(true); return t; });
        Model(String send) { this(send, "weather", false); }
        Model(String send, String callName, boolean answerOnly) { super("gemini-2.0-flash"); this.send = send; this.callName = callName; this.answerOnly = answerOnly; }

        @Override
        public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
            if (send.equals("assembly")) {
                try { post(); } catch (Exception e) { return Flowable.error(e); }
            }
            return Flowable.defer(() -> {
                try {
                    if (request.contents().toString().contains("SLOW-SESSION")) {
                        // An unrelated session whose model call stays open while the others run.
                        ENTERED.countDown();
                        RELEASE.await(60, java.util.concurrent.TimeUnit.SECONDS);
                        post();
                        return Flowable.just(LlmResponse.builder().content(Content.builder().role("model").parts(Part.fromText("SLOW-ANSWER")).build()).build());
                    }
                    if (failingModel) {
                        post();
                        return Flowable.just(LlmResponse.builder().errorMessage("SEED-MODEL-ERROR")
                            .content(Content.builder().role("model").parts(Part.fromText("SEED-FAILED-OUTPUT")).build()).build());
                    }
                    if (send.equals("subscribe")) post();
                    else if (send.equals("executor")) executor.submit(() -> { post(); return null; }).get();
                } catch (Exception e) {
                    return Flowable.error(e);
                }
                boolean answered = answerOnly || request.contents().stream().anyMatch(c -> c.parts().orElse(List.of()).stream().anyMatch(p -> p.functionResponse().isPresent()));
                Content out = answered
                    ? Content.builder().role("model").parts(Part.fromText("SENTINEL-ANSWER-sunny")).build()
                    // The default call names the plain tool, or (scenario "annotated-tool") the annotated one.
                    : Content.builder().role("model").parts(Part.builder().functionCall(FunctionCall.builder().name(callName.equals("weather") ? toolName : callName).args(callName.equals("weather") ? Map.of("city", "Kyoto") : Map.of("request", "Kyoto")).id("call-1").build()).build()).build();
                return Flowable.just(LlmResponse.builder().content(out)
                    .usageMetadata(GenerateContentResponseUsageMetadata.builder().promptTokenCount(11).candidatesTokenCount(7).totalTokenCount(18).build()).build());
            });
        }

        @Override
        public BaseLlmConnection connect(LlmRequest request) { throw new UnsupportedOperationException(); }
    }

    public static void main(String[] args) throws Exception {
        String mode = args[0], prompt = args[2], send = args.length > 3 ? args[3] : "subscribe", scenario = args.length > 4 ? args[4] : "single";
        endpoint = args[1];
        toolCallsModel = scenario.equals("tool-call");
        failingModel = scenario.equals("error");
        if (scenario.equals("annotated-tool")) toolName = "annotatedWeather";
        failingTool = scenario.equals("tool-error");
        InMemorySpanExporter own = InMemorySpanExporter.create();
        if (mode.equals("sdk")) {
            OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(own)).build()).buildAndRegisterGlobal();
        }
        boolean wrapLlm = scenario.equals("wrapped") || scenario.equals("wrapped-llm"); boolean wrapTool = scenario.equals("wrapped") || scenario.equals("wrapped-tool");
        FITracer wrapperTracer = null;
        if (wrapLlm || wrapTool) {
            wrapperSpans = InMemorySpanExporter.create();
            wrapperTracer = new FITracer(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(wrapperSpans)).build().get("wrapper"), TraceConfig.builder().build());
        }
        boolean agentTool = scenario.equals("wrapped-agent-tool"), deferredTool = scenario.equals("wrapped-deferred-tool");
        if (agentTool || deferredTool) {
            wrapTool = true;
            wrapperSpans = InMemorySpanExporter.create();
            wrapperTracer = new FITracer(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(wrapperSpans)).build().get("wrapper"), TraceConfig.builder().build());
        }
        BaseLlm model = wrapLlm ? new TracedLlm(new Model(send), wrapperTracer) : new Model(send, agentTool ? "sub_agent" : "weather", false);
        com.google.adk.tools.BaseTool inner = FunctionTool.create(AdkApp.class, toolName);
        if (agentTool) {
            // A tool that runs a sub-agent: the sub-agent's invocation, agent and model spans start while the tool runs.
            inner = com.google.adk.tools.AgentTool.create(LlmAgent.builder().name("sub_agent").description("a helper agent").model(new Model("none", "weather", true)).instruction("Be brief").build());
        } else if (deferredTool) inner = new DeferredTool();
        com.google.adk.tools.BaseTool weather = wrapTool ? new TracedTool(inner, wrapperTracer) : inner;
        LlmAgent agent = LlmAgent.builder().name("weather_agent").description("answers weather questions").model(model).instruction("Be brief")
            .tools(failingTool ? new FailingTool() : weather).build();
        InMemoryRunner runner = new InMemoryRunner(agent);
        var session = runner.sessionService().createSession(runner.appName(), "user-1").blockingGet();
        Thread slow = null;
        if (scenario.equals("tool-call") || scenario.equals("own-span") || scenario.equals("span-less")) {
            // A second, unrelated session whose model call is open while this one runs.
            var other = runner.sessionService().createSession(runner.appName(), "user-2").blockingGet();
            slow = new Thread(() -> runner.runAsync("user-2", other.id(), Content.fromParts(Part.fromText("SLOW-SESSION"))).toList().blockingGet(), "slow-session");
            slow.start();
            if (!ENTERED.await(60, java.util.concurrent.TimeUnit.SECONDS)) throw new IllegalStateException("the slow session never reached its model");
        }
        if (scenario.equals("span-less")) {
            // Another thread of the application, with no span of its own, makes an LLM HTTP call while the other session's model call is open.
            Thread other = new Thread(() -> { try { String marker = System.getenv("SPANLESS_MARKER_KEY"); post("SPANLESS-B-MARKER test-secret-key " + marker, "?key=" + marker, marker); } catch (Exception e) { throw new IllegalStateException(e); } }, "span-less-b");
            other.start(); other.join();
            RELEASE.countDown();
            slow.join();
            printAndFinish(own);
            return;
        }
        if (scenario.equals("own-span")) {
            // This thread's own application span (mode "sdk"), with an LLM HTTP call under it, while the other session's model call is open.
            var appSpan = io.opentelemetry.api.GlobalOpenTelemetry.getTracer("application").spanBuilder("application-own").startSpan();
            try (var scope = appSpan.makeCurrent()) { post(); } finally { appSpan.end(); }
            RELEASE.countDown();
            slow.join();
            System.out.println("APP-SPAN " + appSpan.getSpanContext().getSpanId());
            printAndFinish(own);
            return;
        }
        List<Event> events = runner.runAsync("user-1", session.id(), Content.fromParts(Part.fromText(prompt))).toList().blockingGet();
        RELEASE.countDown();
        if (slow != null) slow.join();
        System.out.println("EVENTS " + events.size());
        printAndFinish(own);
    }

    /** When set (by {@link #runDeferred}), printAndFinish only records what to print, so an enclosing application span can end first. */
    private static boolean deferFinish;
    private static Runnable deferredFinish;

    /** Runs {@link #main} without the final report and shutdown; call {@link #finish} once the caller's own span has ended. */
    public static void runDeferred(String[] args) throws Exception { deferFinish = true; main(args); }

    public static void finish() { deferredFinish.run(); }

    static void printAndFinish(InMemorySpanExporter own) {
        if (deferFinish) { deferredFinish = () -> report(own); return; }
        report(own);
    }

    private static void report(InMemorySpanExporter own) {
        for (SpanData span : own.getFinishedSpanItems()) {
            // What the application's own provider recorded: ADK's spans, unchanged.
            System.out.println("OWN " + span.getName() + " attributes=" + span.getAttributes().size() + " parent=" + span.getParentSpanContext().isValid());
            System.out.println("OWNID " + span.getName() + " id=" + span.getSpanId() + " parent=" + span.getParentSpanId());
        }
        if (wrapperSpans != null) for (SpanData span : wrapperSpans.getFinishedSpanItems()) {
            // What the wrappers' own provider recorded.
            System.out.println("WRAP " + span.getName() + " id=" + span.getSpanId() + " parent=" + span.getParentSpanId() + " trace=" + span.getTraceId());
        }
        try {
            Class<?> auto = ClassLoader.getSystemClassLoader().loadClass("ai.traceai.auto.TraceAIAuto");
            System.out.println("DOCTOR " + auto.getMethod("doctor").invoke(null).toString().replace('\n', ' '));
            auto.getMethod("shutdown").invoke(null);
        } catch (ReflectiveOperationException e) {
            System.out.println("NO-AGENT");
        }
        System.out.println("DONE");
    }
}
