package ai.traceai.googleadk;

import com.google.adk.agents.LlmAgent;
import com.google.adk.events.Event;
import com.google.adk.models.Gemini;
import com.google.adk.models.LlmRequest;
import com.google.adk.models.LlmResponse;
import com.google.adk.runner.InMemoryRunner;
import com.google.adk.tools.FunctionTool;
import com.google.genai.Client;
import com.google.genai.types.Content;
import com.google.genai.types.HttpOptions;
import com.google.genai.types.Part;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.reactivex.rxjava3.core.Flowable;

import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentSkipListSet;

/**
 * Child process for the javaagent module's GoogleAdkE2ETest: the same agent and tool as {@link AdkApp}, but with ADK's own Gemini model
 * client ({@code com.google.adk.models.Gemini}, built on google-genai), pointed at a local stub through
 * {@code HttpOptions.baseUrl}. Prints which threads subscribed, ran the HTTP call and delivered the answer.
 *
 * <p>Arguments (the same positions as AdkApp, the last two unused): mode ("plain" or "sdk"), the stub's base URL, the prompt.
 */
public class AdkGeminiApp {
    /** A Gemini client that reports the thread it is subscribed on and the thread that delivers its answer. */
    static final class Reporting extends Gemini {
        Reporting(Client client) { super("gemini-2.0-flash", client); }

        @Override
        public Flowable<LlmResponse> generateContent(LlmRequest request, boolean stream) {
            return Flowable.defer(() -> {
                System.out.println("THREAD-SUBSCRIBE " + Thread.currentThread().getName());
                return super.generateContent(request, stream).doOnNext(r -> System.out.println("THREAD-RESPONSE " + Thread.currentThread().getName()));
            });
        }
    }

    public static void main(String[] args) throws Exception {
        String mode = args[0], endpoint = args[1], prompt = args[2];
        InMemorySpanExporter own = InMemorySpanExporter.create();
        if (mode.equals("sdk")) {
            OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(own)).build()).buildAndRegisterGlobal();
        }
        // A thread that samples every thread's stack for the HTTP client's frames: where the model's HTTP call actually runs.
        Set<String> http = new ConcurrentSkipListSet<>();
        Thread sampler = new Thread(() -> {
            while (true) {
                for (var e : Thread.getAllStackTraces().entrySet()) {
                    StackTraceElement[] st = e.getValue();
                    for (StackTraceElement f : st) {
                        // The thread that is inside an OkHttp call, and which call frame it is in (AsyncCall.run: an enqueued call on a dispatcher thread).
                        if (f.getClassName().startsWith("okhttp3.internal.connection.RealCall")) { http.add(e.getKey().getName().replaceAll("http://.*", "") + " in " + f.getClassName().replaceAll(".*\\.", "") + "." + f.getMethodName()); break; }
                    }
                }
                try { Thread.sleep(1); } catch (InterruptedException ex) { return; }
            }
        }, "http-sampler");
        sampler.setDaemon(true);
        sampler.start();

        Client client = Client.builder().apiKey("test-secret-key").httpOptions(HttpOptions.builder().baseUrl(endpoint).build()).build();
        LlmAgent agent = LlmAgent.builder().name("weather_agent").description("answers weather questions").model(new Reporting(client)).instruction("Be brief")
            .tools(FunctionTool.create(AdkApp.class, "weather")).build();
        InMemoryRunner runner = new InMemoryRunner(agent);
        var session = runner.sessionService().createSession(runner.appName(), "user-1").blockingGet();
        List<Event> events = runner.runAsync("user-1", session.id(), Content.fromParts(Part.fromText(prompt))).toList().blockingGet();
        System.out.println("EVENTS " + events.size());
        Thread.sleep(50);
        for (String h : http) System.out.println("THREAD-HTTP " + h);
        AdkApp.printAndFinish(own);
        // google-genai's OkHttp dispatcher threads are not daemons and would keep the JVM up for a minute.
        System.exit(0);
    }
}
