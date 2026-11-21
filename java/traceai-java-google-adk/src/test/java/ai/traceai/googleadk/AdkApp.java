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

    static final class Model extends BaseLlm {
        /** Where the model's HTTP call is sent: "subscribe" (while subscribed), "assembly" (inside generateContent, before the Flowable
         *  exists) or "executor" (on a thread of the client's own executor, which carries no OpenTelemetry context). */
        private final String send;
        private final java.util.concurrent.ExecutorService executor = java.util.concurrent.Executors.newCachedThreadPool(r -> { Thread t = new Thread(r, "model-client"); t.setDaemon(true); return t; });
        Model(String send) { super("gemini-2.0-flash"); this.send = send; }

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
                boolean answered = request.contents().stream().anyMatch(c -> c.parts().orElse(List.of()).stream().anyMatch(p -> p.functionResponse().isPresent()));
                Content out = answered
                    ? Content.builder().role("model").parts(Part.fromText("SENTINEL-ANSWER-sunny")).build()
                    : Content.builder().role("model").parts(Part.builder().functionCall(FunctionCall.builder().name("weather").args(Map.of("city", "Kyoto")).id("call-1").build()).build()).build();
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
        InMemorySpanExporter own = InMemorySpanExporter.create();
        if (mode.equals("sdk")) {
            OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(own)).build()).buildAndRegisterGlobal();
        }
        LlmAgent agent = LlmAgent.builder().name("weather_agent").description("answers weather questions").model(new Model(send)).instruction("Be brief")
            .tools(FunctionTool.create(AdkApp.class, "weather")).build();
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

    static void printAndFinish(InMemorySpanExporter own) {
        for (SpanData span : own.getFinishedSpanItems()) {
            // What the application's own provider recorded: ADK's spans, unchanged.
            System.out.println("OWN " + span.getName() + " attributes=" + span.getAttributes().size() + " parent=" + span.getParentSpanContext().isValid());
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
