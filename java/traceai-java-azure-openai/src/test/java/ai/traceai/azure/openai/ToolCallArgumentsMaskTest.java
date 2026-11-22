package ai.traceai.azure.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.ai.openai.models.ChatCompletionsOptions;
import com.azure.ai.openai.models.ChatRequestUserMessage;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.HttpClient;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpRequest;
import com.azure.core.http.HttpResponse;
import reactor.core.publisher.Mono;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/** Output tool-call arguments are content: the hide settings must mask them. */
class ToolCallArgumentsMaskTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String RESPONSE = "{\"id\":\"resp-1\",\"model\":\"gpt-4o\",\"created\":1,"
        + "\"choices\":[{\"index\":0,\"finish_reason\":\"tool_calls\",\"message\":{\"role\":\"assistant\","
        + "\"content\":null,\"tool_calls\":[{\"id\":\"call-1\",\"type\":\"function\",\"function\":"
        + "{\"name\":\"lookup\",\"arguments\":\"{\\\"ssn\\\":\\\"private-tool-arguments-DO-NOT-EXPORT\\\"}\"}}]}}]}";

    /** A real OpenAIClient (the SDK class is final and cannot be mocked on every JDK) over a canned in-process transport. */
    private static OpenAIClient realClient() {
        HttpClient transport = request -> Mono.just(new CannedResponse(request));
        return new OpenAIClientBuilder()
            .endpoint("https://azure-openai.test")
            .credential(new AzureKeyCredential("synthetic-key-DO-NOT-EXPORT"))
            .httpClient(transport)
            .buildClient();
    }

    private static final class CannedResponse extends HttpResponse {
        private final byte[] body = RESPONSE.getBytes(StandardCharsets.UTF_8);

        CannedResponse(HttpRequest request) {
            super(request);
        }

        @Override public int getStatusCode() { return 200; }
        @Override public String getHeaderValue(String name) { return getHeaders().getValue(name); }
        @Override public HttpHeaders getHeaders() { return new HttpHeaders().set("Content-Type", "application/json"); }
        @Override public Mono<byte[]> getBodyAsByteArray() { return Mono.just(body); }
        @Override public reactor.core.publisher.Flux<ByteBuffer> getBody() { return reactor.core.publisher.Flux.just(ByteBuffer.wrap(body)); }
        @Override public Mono<String> getBodyAsString() { return Mono.just(RESPONSE); }
        @Override public Mono<String> getBodyAsString(Charset charset) { return Mono.just(new String(body, charset)); }
    }

    private SpanData export(TraceConfig config) {
        otel.clearSpans();
        OpenAIClient client = realClient();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
        new TracedAzureOpenAIClient(client, tracer).getChatCompletions("gpt-4o",
            new ChatCompletionsOptions(List.of(new ChatRequestUserMessage("hello"))));
        return otel.getSpans().get(0);
    }

    private static boolean exportsSecret(SpanData data) {
        return data.getAttributes().asMap().values().stream().anyMatch(v -> String.valueOf(v).contains("private-tool-arguments"));
    }

    @Test
    void argumentsAreExportedWhenNothingIsHidden() {
        assertThat(exportsSecret(export(TraceConfig.builder().build()))).isTrue();
    }

    @Test
    void hideOutputsMasksToolCallArguments() {
        assertThat(exportsSecret(export(TraceConfig.builder().hideOutputs(true).build()))).isFalse();
    }

    @Test
    void hideOutputMessagesMasksToolCallArguments() {
        assertThat(exportsSecret(export(TraceConfig.builder().hideOutputMessages(true).build()))).isFalse();
    }

    @Test
    void hideInputsMasksToolCallArguments() {
        // Python lists tool_call.function.arguments on the input side.
        assertThat(exportsSecret(export(TraceConfig.builder().hideInputs(true).build()))).isFalse();
    }

    @Test
    void toolNameStaysVisibleUnderHideInputs() {
        SpanData data = export(TraceConfig.builder().hideInputs(true).build());
        assertThat(data.getAttributes().asMap().values().stream().map(String::valueOf)).contains("lookup");
    }
}
