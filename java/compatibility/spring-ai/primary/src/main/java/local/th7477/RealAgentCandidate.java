package local.th7477;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.spring.TracedChatModel;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.ollama.OllamaChatModel;
import org.springframework.ai.ollama.api.OllamaApi;
import org.springframework.ai.ollama.api.OllamaChatOptions;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.http.client.ClientHttpRequestInterceptor;
import org.springframework.web.client.RestClient;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * After-fix real local agent: ChatClient + OllamaChatModel + a bounded harmless @Tool, through the repaired
 * wrapper. Counts actual outbound /api/chat requests at the transport boundary and denies the fourth before
 * any I/O, because the provider recurses privately for tool continuations.
 */
public final class RealAgentCandidate {
    static final AtomicInteger REQUESTS = new AtomicInteger();
    static final int REQUEST_BUDGET = 3;

    public static final class LocalTools {
        int calls;
        @Tool(description = "Get the current status of the synthetic local service. Returns its exact status code. No network or side effects.")
        public String getLocalStatus() {
            if (++calls > 2) throw new IllegalStateException("Bounded local tool-call budget exceeded");
            System.out.println("ACTUAL_TOOL_EXECUTION getLocalStatus call=" + calls);
            return "LOCAL_OK_7477";
        }
    }

    public static void main(String[] args) {
        String mode = args.length > 0 ? args[0] : "sync";
        String url = System.getenv().getOrDefault("LOCAL_OLLAMA_URL", "http://host.docker.internal:17477");
        ClientHttpRequestInterceptor budget = (request, body, execution) -> {
            if (REQUESTS.incrementAndGet() > REQUEST_BUDGET) {
                throw new IllegalStateException("provider request budget exceeded before I/O");
            }
            return execution.execute(request, body);
        };
        OllamaApi api = OllamaApi.builder().baseUrl(url)
            .restClientBuilder(RestClient.builder().requestInterceptor(budget))
            .webClientBuilder(WebClient.builder().filter((request, next) -> {
                if (REQUESTS.incrementAndGet() > REQUEST_BUDGET) {
                    return reactor.core.publisher.Mono.error(new IllegalStateException("provider request budget exceeded before I/O"));
                }
                return next.exchange(request);
            }))
            .build();
        OllamaChatModel model = OllamaChatModel.builder().ollamaApi(api)
            .defaultOptions(OllamaChatOptions.builder().model("qwen3:1.7b").temperature(0.0).numCtx(4096).numPredict(128)
                .disableThinking().keepAlive("2m").build())
            .build();

        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        FITracer tracer = new FITracer(provider.get("th7477-real-agent"));
        TracedChatModel traced = new TracedChatModel(model, tracer, "ollama");

        LocalTools tools = new LocalTools();
        String prompt = "Call getLocalStatus once to check the local service. Include the exact returned status code in your short answer. Do not guess.";
        ChatClient client = ChatClient.create(traced);
        String answer = "sync".equals(mode)
            ? client.prompt(prompt).tools(tools).call().content()
            : client.prompt(prompt).tools(tools).stream().content().collectList().block().stream().reduce("", String::concat);
        System.out.println("REAL_MODEL_ANSWER=" + answer);

        if (tools.calls < 1 || answer == null || !answer.contains("LOCAL_OK_7477")) {
            throw new AssertionError("Real model tool journey failed; calls=" + tools.calls + ", answer=" + answer);
        }
        List<SpanData> spans = exporter.getFinishedSpanItems();
        boolean sawOutput = spans.stream().anyMatch(s -> "LOCAL_OK_7477".equals(
            s.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)))
            || String.valueOf(s.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).contains("LOCAL_OK_7477"));
        System.out.println("MODE=" + mode + " TOOL_CALLS=" + tools.calls + " PROVIDER_REQUESTS=" + REQUESTS.get()
            + " SPANS=" + spans.size() + " OUTPUT_CAPTURED=" + sawOutput + " REAL_AGENT_PASS");
        if (spans.isEmpty()) throw new AssertionError("no exported spans");
        provider.close();
    }
}
