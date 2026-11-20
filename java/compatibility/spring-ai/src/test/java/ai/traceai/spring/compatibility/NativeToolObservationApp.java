package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.SpringAITracingObservationHandler;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import io.micrometer.core.instrument.observation.DefaultMeterObservationHandler;
import io.micrometer.observation.ObservationRegistry;
import io.micrometer.tracing.handler.DefaultTracingObservationHandler;
import io.micrometer.tracing.otel.bridge.OtelCurrentTraceContext;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Collection;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicInteger;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.tool.function.FunctionToolCallback;

/**
 * The application of NativeToolObservationAgentTest, run in a child JVM with the zero-code agent attached. It makes a real Spring AI
 * tool-calling chat call (DefaultToolCallingManager, OpenAiChatModel against a loopback model) whose tool executes inside Spring AI's own
 * tool_call observation, traced by a real Micrometer tracing handler bridged to the application's OpenTelemetry SDK. Argument one picks the
 * tracing handler: spring (the traceai Spring AI handler), default (Micrometer's DefaultTracingObservationHandler) meter (a real registry with only a metrics handler, no tracing handler) or none (no observation, the no-op registry).
 */
public final class NativeToolObservationApp {
    public record ToolInput(String city) {}
    static final String TOOL_REPLY = ToolExecutionFailureTest.TOOL_REPLY;

    public static void main(String[] args) throws Exception {
        String handler = args[0];
        ConcurrentLinkedQueue<String> appSpans = new ConcurrentLinkedQueue<>();
        SpanExporter printer = new SpanExporter() {
            @Override public CompletableResultCode export(Collection<SpanData> spans) {
                for (SpanData span : spans) appSpans.add(span.getName() + "|" + span.getAttributes().asMap());
                return CompletableResultCode.ofSuccess();
            }
            @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        };
        AtomicInteger requests = new AtomicInteger();
        HttpServer model = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        model.createContext("/", exchange -> {
            exchange.getRequestBody().readAllBytes();
            byte[] body = (requests.getAndIncrement() == 0 ? TOOL_REPLY : NativeSpringAITest.CHAT).getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        model.start();
        AtomicInteger toolCalls = new AtomicInteger();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(printer)).build()) {
            ObservationRegistry registry = handler.equals("none") ? ObservationRegistry.NOOP : ObservationRegistry.create();
            if (handler.equals("meter")) registry.observationConfig().observationHandler(new DefaultMeterObservationHandler(new SimpleMeterRegistry()));
            if (!handler.equals("none") && !handler.equals("meter")) {
                var tracer = new OtelTracer(provider.get("spring-native"), new OtelCurrentTraceContext(), event -> {});
                registry.observationConfig().observationHandler(handler.equals("spring")
                    ? new SpringAITracingObservationHandler(tracer) : new DefaultTracingObservationHandler(tracer));
                if (handler.equals("spring")) registry.observationConfig().observationFilter(new SpringAIObservationFilter());
            }
            var callback = FunctionToolCallback.builder("weather", (ToolInput input) -> { toolCalls.incrementAndGet(); return "private-tool-result"; })
                .description("Synthetic tool").inputType(ToolInput.class).build();
            var prompt = new Prompt("tool input", OpenAiChatOptions.builder().toolCallbacks(callback).build());
            var parent = provider.get("application").spanBuilder("application-parent").startSpan();
            try (var ignored = parent.makeCurrent()) {
                System.out.println("ANSWER=" + Models.toolChat("http://127.0.0.1:" + model.getAddress().getPort(), registry, prompt).getResult().getOutput().getText());
            } finally { parent.end(); }
        } finally { model.stop(0); }
        System.out.println("TOOL_CALLS=" + toolCalls.get() + " MODEL_REQUESTS=" + requests.get());
        for (String span : appSpans) System.out.println("APP_SPAN=" + span);
        Class.forName("ai.traceai.auto.TraceAIAuto").getMethod("flush").invoke(null);
        System.out.println("DONE");
    }
    private NativeToolObservationApp() {}
}
