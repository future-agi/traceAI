package ai.traceai.openai;

import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.LinkedBlockingQueue;

/** A real HTTP boundary shared by the legacy and current SDK privacy regressions. */
final class OpenAILoopback implements AutoCloseable {
    static final String CHAT_USAGE = "\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15,"
        + "\"prompt_tokens_details\":{\"cached_tokens\":6},\"completion_tokens_details\":{\"reasoning_tokens\":2}}";
    static final String CHAT = "{\"id\":\"chat-test\",\"object\":\"chat.completion\",\"created\":1,\"model\":\"served-model\","
        + "\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"private-answer\"},\"finish_reason\":\"stop\"}],"
        + CHAT_USAGE + "}";
    static final String CHAT_SSE = "data: {\"id\":\"chat-test\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"served-model\","
        + "\"choices\":[{\"index\":0,\"delta\":{\"content\":\"private-answer\"},\"finish_reason\":\"stop\"}]}\n\n"
        + "data: {\"id\":\"chat-test\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"served-model\",\"choices\":[],"
        + CHAT_USAGE + "}\n\ndata: [DONE]\n\n";
    static final String EMBEDDING = "{\"object\":\"list\",\"model\":\"served-model\",\"data\":[{\"object\":\"embedding\",\"index\":0,"
        + "\"embedding\":[0.1,0.2]}],\"usage\":{\"prompt_tokens\":10,\"total_tokens\":10}}";
    static final String RESPONSE = "{\"id\":\"resp-test\",\"object\":\"response\",\"created_at\":1,\"model\":\"served-model\",\"status\":\"completed\","
        + "\"output\":[{\"type\":\"message\",\"id\":\"msg-test\",\"role\":\"assistant\",\"status\":\"completed\","
        + "\"content\":[{\"type\":\"output_text\",\"text\":\"private-answer\",\"annotations\":[]}]}],"
        + "\"usage\":{\"input_tokens\":10,\"output_tokens\":5,\"total_tokens\":15,"
        + "\"input_tokens_details\":{\"cached_tokens\":6},\"output_tokens_details\":{\"reasoning_tokens\":2}}}";
    static final String RESPONSE_SSE = "event: response.completed\ndata: {\"type\":\"response.completed\",\"sequence_number\":1,\"response\":"
        + RESPONSE + "}\n\n";

    final LinkedBlockingQueue<String> requests = new LinkedBlockingQueue<>();
    private final HttpServer server;

    OpenAILoopback(String contentType, String response) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/", exchange -> {
            requests.add(exchange.getRequestHeaders().entrySet().toString() + " " + exchange.getRequestURI()
                + " " + new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", contentType);
            exchange.sendResponseHeaders(200, bytes.length);
            try (var out = exchange.getResponseBody()) { out.write(bytes); }
            finally { exchange.close(); }
        });
        server.start();
    }

    String url() { return "http://127.0.0.1:" + server.getAddress().getPort() + "/v1"; }
    @Override public void close() { server.stop(0); }
}
