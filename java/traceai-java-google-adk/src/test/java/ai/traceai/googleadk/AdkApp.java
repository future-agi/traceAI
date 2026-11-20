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
 * <p>Arguments: mode ("plain" or "sdk", the application installs its own OpenTelemetry SDK), the model endpoint, the prompt.
 */
public class AdkApp {
    public static Map<String, Object> weather(@Schema(name = "city") String city) {
        return Map.of("city", city, "forecast", "SENTINEL-TOOL-RESULT");
    }

    static final class Model extends BaseLlm {
        private final String endpoint;
        Model(String endpoint) { super("gemini-2.0-flash"); this.endpoint = endpoint; }

        @Override
        public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
            return Flowable.defer(() -> {
                try {
                    HttpRequest http = HttpRequest.newBuilder(URI.create(endpoint + "/v1/chat/completions")).header("content-type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString("{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}")).build();
                    HttpClient.newHttpClient().send(http, HttpResponse.BodyHandlers.ofString());
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
        String mode = args[0], endpoint = args[1], prompt = args[2];
        InMemorySpanExporter own = InMemorySpanExporter.create();
        if (mode.equals("sdk")) {
            OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(own)).build()).buildAndRegisterGlobal();
        }
        LlmAgent agent = LlmAgent.builder().name("weather_agent").description("answers weather questions").model(new Model(endpoint)).instruction("Be brief")
            .tools(FunctionTool.create(AdkApp.class, "weather")).build();
        InMemoryRunner runner = new InMemoryRunner(agent);
        var session = runner.sessionService().createSession(runner.appName(), "user-1").blockingGet();
        List<Event> events = runner.runAsync("user-1", session.id(), Content.fromParts(Part.fromText(prompt))).toList().blockingGet();
        System.out.println("EVENTS " + events.size());
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
