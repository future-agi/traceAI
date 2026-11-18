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
import org.springframework.ai.tool.definition.ToolDefinition;
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
 * spring_observed_untraced (the same observations without a tracing handler).
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

    /** A decorator that delegates every call to the callback it wraps, as tool-filtering wrappers do. */
    static final class WrappingCallback implements ToolCallback {
        final ToolCallback delegate;
        WrappingCallback(ToolCallback delegate) { this.delegate = delegate; }
        @Override public ToolDefinition getToolDefinition() { return delegate.getToolDefinition(); }
        @Override public String call(String input) { return delegate.call(input); }
    }

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
                boolean fail = scenario.equals("spring_error");
                Function<WeatherRequest, String> weather = input -> {
                    try { chat(); } catch (Exception error) { throw new IllegalStateException(error); }
                    if (fail) throw new IllegalStateException(ERROR);
                    return "sunny in " + input.city();
                };
                ToolCallback callback = FunctionToolCallback.builder("weather", weather).description("Looks up the weather").inputType(WeatherRequest.class).build();
                String input = "{\"city\":\"" + CITY + "\",\"apiKey\":\"" + SECRET + "\"}";
                if (scenario.equals("spring_wrapper")) callback = new WrappingCallback(callback);
                try {
                    if (scenario.startsWith("spring_observed")) System.out.println("RESULT=" + observed(provider, callback, input, !scenario.endsWith("_untraced")));
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
                + "|" + span.getAttributes().get(AttributeKey.stringKey("gen_ai.span.kind")) + "|" + span.getSpanId() + "|" + span.getParentSpanId());
        // Control: a provider call outside every tool.
        chat();
        System.out.println("DONE");
    }
    /** One model-requested tool call through Spring AI's tool calling manager, observed and bridged to the application's SDK. */
    static String observed(SdkTracerProvider provider, ToolCallback callback, String input, boolean traced) {
        ObservationRegistry registry = ObservationRegistry.create();
        // Untraced: observations run (for example for metrics) but no tracing handler turns them into spans.
        registry.observationConfig().observationHandler(context -> true).observationFilter(new SpringAIObservationFilter(true, true));
        if (traced) registry.observationConfig().observationHandler(
            new SpringAITracingObservationHandler(new OtelTracer(provider.get("spring-ai"), new OtelCurrentTraceContext(), event -> { })));
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
