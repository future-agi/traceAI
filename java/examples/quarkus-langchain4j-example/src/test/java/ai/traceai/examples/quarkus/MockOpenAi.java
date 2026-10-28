package ai.traceai.examples.quarkus;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * A scripted OpenAI-compatible {@code /v1/chat/completions} server shaped like the documented Chat Completions API.
 * <ul>
 * <li>"... weather in CITY?": first call answers with a {@code currentWeather} tool call, the call after the tool
 * result answers with text that repeats the tool result.</li>
 * <li>A question containing {@code MODEL-FAIL} gets HTTP 500 whose error body repeats the question.</li>
 * <li>A question containing {@code NO-USAGE} gets responses without a {@code usage} object.</li>
 * </ul>
 */
final class MockOpenAi implements AutoCloseable {

    static final int TOOL_CALL_PROMPT_TOKENS = 31;
    static final int TOOL_CALL_COMPLETION_TOKENS = 7;
    static final int ANSWER_PROMPT_TOKENS = 52;
    static final int ANSWER_COMPLETION_TOKENS = 11;
    private static final Pattern CITY = Pattern.compile("weather in (\\S+?)\\?");

    record Request(Map<String, String> headers, String body) {
    }

    private final ObjectMapper json = new ObjectMapper();
    private final HttpServer server;
    private final List<Request> requests = new CopyOnWriteArrayList<>();

    MockOpenAi() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/chat/completions", this::handle);
        server.start();
    }

    private void handle(HttpExchange exchange) throws IOException {
        Map<String, String> headers = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
        exchange.getRequestHeaders().forEach((k, v) -> headers.put(k, String.join(",", v)));
        String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        requests.add(new Request(headers, body));

        JsonNode messages = json.readTree(body).path("messages");
        JsonNode last = messages.get(messages.size() - 1);
        String question = "";
        for (JsonNode m : messages) {
            if ("user".equals(m.path("role").asText())) {
                question = m.path("content").asText();
            }
        }
        boolean usage = !question.contains("NO-USAGE");

        if (question.contains("MODEL-FAIL")) {
            ObjectNode error = json.createObjectNode();
            error.putObject("error").put("message", "upstream rejected the request: " + question).put("type", "server_error");
            send(exchange, 500, error);
            return;
        }

        ObjectNode response = json.createObjectNode();
        response.put("id", "chatcmpl-test-" + requests.size());
        response.put("object", "chat.completion");
        response.put("created", 1760000000L);
        response.put("model", "gpt-test-2026-10-01");
        ObjectNode choice = response.putArray("choices").addObject();
        choice.put("index", 0);
        ObjectNode message = choice.putObject("message");
        message.put("role", "assistant");

        if ("tool".equals(last.path("role").asText())) {
            message.put("content", "Here you go: " + last.path("content").asText());
            choice.put("finish_reason", "stop");
            if (usage) {
                usage(response, ANSWER_PROMPT_TOKENS, ANSWER_COMPLETION_TOKENS);
            }
        } else {
            Matcher m = CITY.matcher(question);
            String city = m.find() ? m.group(1) : "Paris";
            message.putNull("content");
            ArrayNode calls = message.putArray("tool_calls");
            ObjectNode call = calls.addObject();
            call.put("id", "call_" + requests.size());
            call.put("type", "function");
            call.putObject("function").put("name", "currentWeather")
                    .put("arguments", json.createObjectNode().put("city", city).toString());
            choice.put("finish_reason", "tool_calls");
            if (usage) {
                usage(response, TOOL_CALL_PROMPT_TOKENS, TOOL_CALL_COMPLETION_TOKENS);
            }
        }
        send(exchange, 200, response);
    }

    private static void usage(ObjectNode response, int prompt, int completion) {
        response.putObject("usage").put("prompt_tokens", prompt).put("completion_tokens", completion)
                .put("total_tokens", prompt + completion);
    }

    private void send(HttpExchange exchange, int status, JsonNode body) throws IOException {
        byte[] bytes = json.writeValueAsBytes(body);
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    String baseUrl() {
        return "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/";
    }

    List<Request> requests() {
        return new ArrayList<>(requests);
    }

    void clear() {
        requests.clear();
    }

    @Override
    public void close() {
        server.stop(0);
    }
}
