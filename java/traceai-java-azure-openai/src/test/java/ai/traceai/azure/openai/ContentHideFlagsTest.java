package ai.traceai.azure.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.ai.openai.models.ChatCompletionsOptions;
import com.azure.ai.openai.models.ChatRequestSystemMessage;
import com.azure.ai.openai.models.ChatRequestUserMessage;
import com.azure.ai.openai.models.CompletionsOptions;
import com.azure.ai.openai.models.EmbeddingsOptions;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.HttpClient;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpRequest;
import com.azure.core.http.HttpResponse;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Prompt, response, tool-call arguments and embedding text must not be exported under the hide flags,
 * on every Azure OpenAI path (chat, completions, embeddings). Offline: a real OpenAIClient over a canned in-process transport.
 */
class ContentHideFlagsTest {
    private static final String IN = "SENTINEL-INPUT-PROMPT-DO-NOT-EXPORT";
    private static final String OUT = "SENTINEL-OUTPUT-RESPONSE-DO-NOT-EXPORT";
    private static final String ARGS = "SENTINEL-TOOL-ARGS-DO-NOT-EXPORT";

    private static final String CHAT = "{\"id\":\"resp-1\",\"model\":\"gpt-4o\",\"created\":1,"
        + "\"choices\":[{\"index\":0,\"finish_reason\":\"tool_calls\",\"message\":{\"role\":\"assistant\","
        + "\"content\":\"" + OUT + "\",\"tool_calls\":[{\"id\":\"call-1\",\"type\":\"function\",\"function\":"
        + "{\"name\":\"lookup\",\"arguments\":\"{\\\"q\\\":\\\"" + ARGS + "\\\"}\"}}]}}],"
        + "\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1,\"total_tokens\":2}}";
    private static final String COMPLETIONS = "{\"id\":\"c-1\",\"created\":1,\"choices\":[{\"text\":\"" + OUT
        + "\",\"index\":0,\"finish_reason\":\"stop\"}],"
        + "\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1,\"total_tokens\":2}}";
    private static final String EMBEDDINGS = "{\"data\":[{\"embedding\":[0.1,0.2],\"index\":0}],"
        + "\"usage\":{\"prompt_tokens\":1,\"total_tokens\":1}}";

    /**
     * A real OpenAIClient (the SDK class is final and cannot be mocked on every JDK) over a canned
     * in-process transport that answers each operation by its request path.
     */
    private static OpenAIClient realClient() {
        HttpClient transport = request -> Mono.just(new CannedResponse(request));
        return new OpenAIClientBuilder()
            .endpoint("https://azure-openai.test")
            .credential(new AzureKeyCredential("synthetic-key-DO-NOT-EXPORT"))
            .httpClient(transport)
            .buildClient();
    }

    private static String cannedBody(HttpRequest request) {
        String path = request.getUrl().getPath();
        if (path.endsWith("/chat/completions")) return CHAT;
        if (path.endsWith("/embeddings")) return EMBEDDINGS;
        if (path.endsWith("/completions")) return COMPLETIONS;
        throw new IllegalStateException("no canned response for request path " + path);
    }

    private static final class CannedResponse extends HttpResponse {
        private final String text;
        private final byte[] body;

        CannedResponse(HttpRequest request) {
            super(request);
            this.text = cannedBody(request);
            this.body = text.getBytes(StandardCharsets.UTF_8);
        }

        @Override public int getStatusCode() { return 200; }
        @Override public String getHeaderValue(String name) { return getHeaders().getValue(name); }
        @Override public HttpHeaders getHeaders() { return new HttpHeaders().set("Content-Type", "application/json"); }
        @Override public Mono<byte[]> getBodyAsByteArray() { return Mono.just(body); }
        @Override public Flux<ByteBuffer> getBody() { return Flux.just(ByteBuffer.wrap(body)); }
        @Override public Mono<String> getBodyAsString() { return Mono.just(text); }
        @Override public Mono<String> getBodyAsString(Charset charset) { return Mono.just(new String(body, charset)); }
    }

    private static List<SpanData> run(TraceConfig config, Function<TracedAzureOpenAIClient, Object> call) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            OpenAIClient client = realClient();
            call.apply(new TracedAzureOpenAIClient(client, new FITracer(provider.get("t"), config)));
            return exporter.getFinishedSpanItems();
        } finally {
            provider.close();
        }
    }

    private static List<String> everything(List<SpanData> spans) {
        List<String> all = new ArrayList<>();
        for (SpanData span : spans) {
            span.getAttributes().forEach((k, v) -> all.add(k.getKey() + "=" + v));
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> all.add(k.getKey() + "=" + v)));
        }
        return all;
    }

    private static void assertAbsent(List<SpanData> spans, String... secrets) {
        assertThat(spans).isNotEmpty();
        List<String> leaked = new ArrayList<>();
        for (String entry : everything(spans)) {
            for (String secret : secrets) {
                if (entry.contains(secret)) leaked.add(entry.substring(0, entry.indexOf('=')));
            }
        }
        assertThat(leaked).as("content keys exported with a sentinel value").isEmpty();
    }

    private static Object chat(TracedAzureOpenAIClient c) {
        return c.getChatCompletions("gpt-4o", new ChatCompletionsOptions(
            List.of(new ChatRequestSystemMessage("be brief"), new ChatRequestUserMessage(IN))));
    }

    private static Object completions(TracedAzureOpenAIClient c) {
        return c.getCompletions("gpt-4o", new CompletionsOptions(List.of(IN)));
    }

    private static Object embeddings(TracedAzureOpenAIClient c) {
        return c.getEmbeddings("emb", new EmbeddingsOptions(List.of(IN)));
    }

    @Test
    void controlExportsContentWhenNothingIsHidden() {
        TraceConfig none = TraceConfig.builder().build();
        assertThat(everything(run(none, ContentHideFlagsTest::chat))).anyMatch(s -> s.contains(IN));
        assertThat(everything(run(none, ContentHideFlagsTest::chat))).anyMatch(s -> s.contains(OUT));
        assertThat(everything(run(none, ContentHideFlagsTest::embeddings))).anyMatch(s -> s.contains(IN));
    }

    @ParameterizedTest(name = "{0} on {1}")
    @CsvSource({
        "hideInputs,chat", "hideInputs,completions", "hideInputs,embeddings",
        "hideInputMessages,chat", "hideInputMessages,completions", "hideInputMessages,embeddings",
        "hideOutputs,chat", "hideOutputs,completions",
        "hideOutputMessages,chat", "hideOutputMessages,completions"})
    void hideFlagKeepsContentOutOfEverySpan(String flag, String path) {
        boolean input = flag.startsWith("hideInput");
        TraceConfig.Builder b = TraceConfig.builder();
        switch (flag) {
            case "hideInputs" -> b.hideInputs(true);
            case "hideInputMessages" -> b.hideInputMessages(true);
            case "hideOutputs" -> b.hideOutputs(true);
            default -> b.hideOutputMessages(true);
        }
        Function<TracedAzureOpenAIClient, Object> call = switch (path) {
            case "chat" -> ContentHideFlagsTest::chat;
            case "completions" -> ContentHideFlagsTest::completions;
            default -> ContentHideFlagsTest::embeddings;
        };
        // Tool-call arguments are Python input-side content; Java hides them under any of the four flags.
        if (path.equals("chat")) {
            assertAbsent(run(b.build(), call), input ? IN : OUT, ARGS);
        } else {
            assertAbsent(run(b.build(), call), input ? IN : OUT);
        }
    }
}
