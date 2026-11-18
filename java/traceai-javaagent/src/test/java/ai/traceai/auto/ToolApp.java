package ai.traceai.auto;

import dev.langchain4j.agent.tool.Tool;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.agent.tool.ToolSpecification;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.ToolExecutionResultMessage;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.output.Response;
import dev.langchain4j.service.AiServices;
import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.SpringAITracingObservationHandler;
import io.micrometer.observation.ObservationRegistry;
import io.micrometer.tracing.handler.DefaultTracingObservationHandler;
import io.micrometer.tracing.otel.bridge.OtelCurrentTraceContext;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.model.tool.DefaultToolCallingManager;
import org.springframework.ai.model.tool.ToolCallingChatOptions;
import org.springframework.ai.model.tool.ToolExecutionResult;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.function.FunctionToolCallback;
import java.net.URI;
import java.net.http.*;
import java.util.List;
import java.util.function.Function;

/**
 * A plain application that runs real framework tool loops: no agent API calls, no tracer calls.
 * Each tool makes one provider call of its own, and the application makes one more control call
 * after the tool loop. Everything runs under an application-owned span.
 * Scenarios: lc4j, lc4j_error, spring, spring_error, spring_wrapper (a ToolCallback decorating
 * another one), spring_observed (DefaultToolCallingManager with Spring AI's tool_call observation
 * bridged to the application's OpenTelemetry SDK by Micrometer Tracing and traceai-spring-ai),
 * spring_observed_untraced (the same observations without a tracing handler), spring_observed_nested
 * (the observed tool calls a different tool, which calls a second weather callback directly),
 * spring_observed_nofilter[_error] (Micrometer Tracing's default handler only, no traceai-spring-ai),
 * spring_overload (an unrelated call(String, String) overload, which is not a tool call).
 * The application's own spans are exported in memory and printed as APP_SPAN lines.
 */
public final class ToolApp {
    /** Arguments carry a credential-shaped value that must never be exported raw. */
    static final String SECRET = "sk-proj-TOOLSECRET0123456789abcdefABCDEF";
    static final String CITY = "Paris";
    static final String RESULT = "sunny in Paris";
    static final String ERROR = "tool exploded for Paris";
    static String endpoint;

    public interface Assistant { String chat(String message); }

    public static final class Tools {
        final boolean fail;
        Tools(boolean fail) { this.fail = fail; }
        @Tool("Looks up the weather for a city") public String lookup(String city) throws Exception {
            chat();
            if (fail) throw new IllegalStateException(ERROR);
            return "sunny in " + city;
        }
    }

    /** First turn requests one tool call; the second turn answers with the tool result. */
    static final class FakeModel implements ChatLanguageModel {
        final String arguments;
        FakeModel(String arguments) { this.arguments = arguments; }
        @Override public Response<AiMessage> generate(List<ChatMessage> messages) { return generate(messages, List.of()); }
        @Override public Response<AiMessage> generate(List<ChatMessage> messages, List<ToolSpecification> tools) {
            ChatMessage last = messages.get(messages.size() - 1);
            if (last instanceof ToolExecutionResultMessage result) return Response.from(AiMessage.from("answer: " + result.text()));
            return Response.from(AiMessage.from(ToolExecutionRequest.builder().id("call-1").name("lookup").arguments(arguments).build()));
        }
    }

    public record WeatherRequest(String city, String apiKey) { }

    static void chat() throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(endpoint)).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(PlainApp.REQUEST)).build();
        HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofString());
    }

    public static void main(String[] args) throws Exception {
        endpoint = args[0]; String scenario = args[1];
        InMemorySpanExporter applicationSpans = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(applicationSpans)).build();
        Span parent = provider.get("tool-app").spanBuilder("application-request").startSpan();
        try (Scope ignored = parent.makeCurrent()) {
            System.out.println("PARENT=" + parent.getSpanContext().getSpanId());
            System.out.println("TRACE=" + parent.getSpanContext().getTraceId());
            if (scenario.startsWith("lc4j")) {
                String key = Tools.class.getMethod("lookup", String.class).getParameters()[0].getName();
                String arguments = "{\"" + key + "\":\"" + CITY + "\",\"api_key\":\"" + SECRET + "\"}";
                Assistant assistant = AiServices.builder(Assistant.class).chatLanguageModel(new FakeModel(arguments))
                    .tools(new Tools(scenario.equals("lc4j_error"))).build();
                System.out.println("RESULT=" + assistant.chat("What is the weather?"));
            } else {
                boolean fail = scenario.endsWith("_error");
                Function<WeatherRequest, String> weather = input -> {
                    try { chat(); } catch (Exception error) { throw new IllegalStateException(error); }
                    if (fail) throw new IllegalStateException(ERROR);
                    return "sunny in " + input.city();
                };
                ToolCallback callback = FunctionToolCallback.builder("weather", weather).description("Looks up the weather").inputType(WeatherRequest.class).build();
                String input = "{\"city\":\"" + CITY + "\",\"apiKey\":\"" + SECRET + "\"}";
                if (scenario.equals("spring_wrapper")) callback = new ai.traceai.tooltest.Callbacks.Wrapping(callback);
                if (scenario.equals("spring_observed_nested")) {
                    // weather (observed) -> forecast (direct) -> weather (direct, a different callback): the
                    // two direct calls are not the observed invocation and keep their own agent spans.
                    Function<WeatherRequest, String> inner = request -> "sunny in " + request.city();
                    ToolCallback innerWeather = FunctionToolCallback.builder("weather", inner).description("Looks up the weather").inputType(WeatherRequest.class).build();
                    Function<WeatherRequest, String> forecastTool = request -> innerWeather.call("{\"city\":\"" + request.city() + "\"}");
                    ToolCallback forecast = FunctionToolCallback.builder("forecast", forecastTool).description("Forecast").inputType(WeatherRequest.class).build();
                    Function<WeatherRequest, String> outer = request -> forecast.call("{\"city\":\"" + request.city() + "\"}");
                    callback = FunctionToolCallback.builder("weather", outer).description("Looks up the weather").inputType(WeatherRequest.class).build();
                }
                try {
                    if (scenario.startsWith("spring_observed")) System.out.println("RESULT=" + observed(provider, callback, input,
                        scenario.contains("_untraced") ? "untraced" : scenario.contains("_nofilter") ? "nofilter" : "traceai"));
                    else if (scenario.equals("spring_overload")) System.out.println("RESULT=" + new ai.traceai.tooltest.Callbacks.Overloaded(callback).call(input, "not a ToolContext"));
                    else System.out.println("RESULT=" + callback.call(input));
                }
                catch (RuntimeException error) {
                    System.out.println("THROWN=" + error.getClass().getName() + "|" + error.getMessage()
                        + "|" + (error.getCause() == null ? "" : error.getCause().getClass().getName() + ":" + error.getCause().getMessage()));
                }
            }
        } finally { parent.end(); }
        for (SpanData span : applicationSpans.getFinishedSpanItems())
            System.out.println("APP_SPAN=" + span.getName() + "|" + span.getAttributes().get(AttributeKey.stringKey("fi.span.kind"))
                + "|" + span.getAttributes().get(AttributeKey.stringKey("gen_ai.span.kind")) + "|" + span.getSpanId() + "|" + span.getParentSpanId()
                + "|" + span.getAttributes().get(AttributeKey.stringKey("gen_ai.tool.name")) + "|" + span.getAttributes().get(AttributeKey.stringKey("gen_ai.tool.call.arguments"))
                + "|" + span.getAttributes().get(AttributeKey.stringKey("gen_ai.tool.call.result")) + "|" + span.getAttributes().get(AttributeKey.stringKey("error.type")));
        // Control: a provider call outside every tool.
        chat();
        System.out.println("DONE");
    }
    /** One model-requested tool call through Spring AI's tool calling manager, observed and bridged to the application's SDK. */
    static String observed(SdkTracerProvider provider, ToolCallback callback, String input, String mode) {
        ObservationRegistry registry = ObservationRegistry.create();
        OtelTracer tracer = new OtelTracer(provider.get("spring-ai"), new OtelCurrentTraceContext(), event -> { });
        if (mode.equals("nofilter")) {
            // Zero-code: Micrometer Tracing's default handler (as Spring Boot actuator registers it), no traceai-spring-ai.
            registry.observationConfig().observationHandler(new DefaultTracingObservationHandler(tracer));
        } else {
            // Untraced: observations run (for example for metrics) but no tracing handler turns them into spans.
            registry.observationConfig().observationHandler(context -> true).observationFilter(new SpringAIObservationFilter(true, true));
            if (mode.equals("traceai")) registry.observationConfig().observationHandler(new SpringAITracingObservationHandler(tracer));
        }
        DefaultToolCallingManager manager = DefaultToolCallingManager.builder().observationRegistry(registry).build();
        Prompt prompt = new Prompt(List.of(new UserMessage("What is the weather?")), ToolCallingChatOptions.builder().toolCallbacks(callback).build());
        AssistantMessage request = AssistantMessage.builder().content("")
            .toolCalls(List.of(new AssistantMessage.ToolCall("call-1", "function", "weather", input))).build();
        ToolExecutionResult result = manager.executeToolCalls(prompt, new ChatResponse(List.of(new Generation(request))));
        List<org.springframework.ai.chat.messages.Message> history = result.conversationHistory();
        return ((org.springframework.ai.chat.messages.ToolResponseMessage)history.get(history.size() - 1)).getResponses().get(0).responseData();
    }
    private ToolApp() {}
}
