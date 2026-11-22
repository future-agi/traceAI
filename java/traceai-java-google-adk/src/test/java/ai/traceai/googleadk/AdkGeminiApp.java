package ai.traceai.googleadk;

import com.google.adk.agents.LlmAgent;
import com.google.adk.events.Event;
import com.google.adk.models.Gemini;
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

import java.util.List;

/**
 * Child process for the javaagent module's GoogleAdkE2ETest: the same agent and tool as {@link AdkApp}, but with ADK's own Gemini model
 * client ({@code com.google.adk.models.Gemini}, built on google-genai), pointed at a local stub through
 * {@code HttpOptions.baseUrl}.
 *
 * <p>Arguments (the same positions as AdkApp, the last two unused): mode ("plain" or "sdk"), the stub's base URL, the prompt.
 */
public class AdkGeminiApp {
    /** ADK's own Gemini client, unchanged. */
    static final class Reporting extends Gemini {
        Reporting(Client client) { super("gemini-2.0-flash", client); }
    }

    public static void main(String[] args) throws Exception {
        String mode = args[0], endpoint = args[1], prompt = args[2];
        InMemorySpanExporter own = InMemorySpanExporter.create();
        if (mode.equals("sdk")) {
            OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(own)).build()).buildAndRegisterGlobal();
        }
        Client client = Client.builder().apiKey("test-secret-key").httpOptions(HttpOptions.builder().baseUrl(endpoint).build()).build();
        LlmAgent agent = LlmAgent.builder().name("weather_agent").description("answers weather questions").model(new Reporting(client)).instruction("Be brief")
            .tools(FunctionTool.create(AdkApp.class, "weather")).build();
        InMemoryRunner runner = new InMemoryRunner(agent);
        var session = runner.sessionService().createSession(runner.appName(), "user-1").blockingGet();
        List<Event> events = runner.runAsync("user-1", session.id(), Content.fromParts(Part.fromText(prompt))).toList().blockingGet();
        System.out.println("EVENTS " + events.size());
        AdkApp.printAndFinish(own);
        // google-genai's OkHttp dispatcher threads are not daemons and would keep the JVM up for a minute.
        System.exit(0);
    }
}
