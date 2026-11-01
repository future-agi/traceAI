package ai.traceai.semantickernel;

import com.azure.ai.openai.OpenAIAsyncClient;
import com.azure.ai.openai.OpenAIClientBuilder;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.core.http.HttpHeaderName;
import com.azure.core.http.HttpHeaders;
import com.azure.core.http.HttpRequest;
import com.azure.core.http.HttpResponse;
import com.azure.core.http.policy.FixedDelayOptions;
import com.azure.core.http.policy.RetryOptions;
import com.microsoft.semantickernel.aiservices.openai.chatcompletion.OpenAIChatCompletion;
import com.microsoft.semantickernel.services.chatcompletion.ChatCompletionService;
import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * An in-process stand-in for the Azure OpenAI chat endpoint, plugged in as the SDK's HTTP client.
 * Requests never reach a socket.
 */
final class LocalOpenAi implements AutoCloseable {

    enum Mode { OK, SERVER_ERROR, HANG, TOOL, STREAM }

    static final String ANSWER = "synthetic-model-answer";
    static final String ERROR_DETAIL = "synthetic-upstream-error-detail";

    final CountDownLatch received = new CountDownLatch(1);
    final CountDownLatch cancelled = new CountDownLatch(1);
    final AtomicInteger requests = new AtomicInteger();
    private volatile Mode mode = Mode.OK;

    LocalOpenAi mode(Mode mode) {
        this.mode = mode;
        return this;
    }

    ChatCompletionService chatService() {
        OpenAIAsyncClient client = new OpenAIClientBuilder()
            .endpoint("https://openai.invalid")
            .credential(new AzureKeyCredential("synthetic-key"))
            .retryOptions(new RetryOptions(new FixedDelayOptions(0, Duration.ofMillis(1))))
            .httpClient(this::send)
            .buildAsyncClient();
        return OpenAIChatCompletion.builder()
            .withOpenAIAsyncClient(client)
            .withModelId("gpt-local")
            .withDeploymentName("gpt-local")
            .build();
    }

    private Mono<HttpResponse> send(HttpRequest request) {
        return Mono.defer(() -> {
            int index = requests.incrementAndGet();
            received.countDown();
            if (mode == Mode.HANG) {
                return Mono.<HttpResponse>never().doOnCancel(cancelled::countDown);
            }
            if (mode == Mode.SERVER_ERROR) {
                return Mono.just(new Response(request, 500,
                    "{\"error\":{\"message\":\"" + ERROR_DETAIL + "\",\"type\":\"server_error\"}}"));
            }
            if (mode == Mode.STREAM) {
                String event = "data: {\"id\":\"chatcmpl-stream\",\"object\":\"chat.completion.chunk\","
                    + "\"created\":1700000000,\"model\":\"gpt-local\",\"choices\":[{\"index\":0,"
                    + "\"delta\":{\"role\":\"assistant\",\"content\":\"" + ANSWER
                    + "\"},\"finish_reason\":null}]}\n\n";
                Response response = new Response(request, 200, event + "data: [DONE]\n\n");
                response.headers.set(HttpHeaderName.CONTENT_TYPE, "text/event-stream");
                return Mono.just(response);
            }
            if (mode == Mode.TOOL && index == 1) {
                return Mono.just(new Response(request, 200,
                    "{\"id\":\"chatcmpl-tool\",\"object\":\"chat.completion\",\"created\":1700000000,"
                        + "\"model\":\"gpt-local\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\","
                        + "\"content\":null,\"tool_calls\":[{\"id\":\"call_local\",\"type\":\"function\","
                        + "\"function\":{\"name\":\"local-immediate\",\"arguments\":\"{}\"}}]},"
                        + "\"finish_reason\":\"tool_calls\"}],"
                        + "\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":3,\"total_tokens\":10}}"));
            }
            return Mono.just(new Response(request, 200,
                "{\"id\":\"chatcmpl-local\",\"object\":\"chat.completion\",\"created\":1700000000,"
                    + "\"model\":\"gpt-local\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\","
                    + "\"content\":\"" + ANSWER + "\"},\"finish_reason\":\"stop\"}],"
                    + "\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":3,\"total_tokens\":10}}"));
        });
    }

    @Override
    public void close() {
    }

    private static final class Response extends HttpResponse {
        private final int status;
        private final byte[] body;
        private final HttpHeaders headers = new HttpHeaders()
            .set(HttpHeaderName.CONTENT_TYPE, "application/json");

        Response(HttpRequest request, int status, String body) {
            super(request);
            this.status = status;
            this.body = body.getBytes(StandardCharsets.UTF_8);
        }

        @Override
        public int getStatusCode() {
            return status;
        }

        @Override
        @Deprecated
        public String getHeaderValue(String name) {
            return headers.getValue(name);
        }

        @Override
        public HttpHeaders getHeaders() {
            return headers;
        }

        @Override
        public Flux<ByteBuffer> getBody() {
            return Flux.defer(() -> Flux.just(ByteBuffer.wrap(body)));
        }

        @Override
        public Mono<byte[]> getBodyAsByteArray() {
            return Mono.fromSupplier(body::clone);
        }

        @Override
        public Mono<String> getBodyAsString() {
            return getBodyAsString(StandardCharsets.UTF_8);
        }

        @Override
        public Mono<String> getBodyAsString(Charset charset) {
            return Mono.fromSupplier(() -> new String(body, charset));
        }
    }
}
