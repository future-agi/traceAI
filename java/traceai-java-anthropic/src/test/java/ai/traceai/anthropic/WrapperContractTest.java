package ai.traceai.anthropic;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.errors.BadRequestException;
import com.anthropic.core.ObjectMappers;
import com.fasterxml.jackson.databind.JsonNode;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static ai.traceai.anthropic.SdkTestSupport.*;
import static org.junit.jupiter.api.Assertions.*;

/** Real SDK contracts against synthetic loopback HTTP, on both the SDK floor and latest. */
class WrapperContractTest {
    private static final String INPUT = "synthetic-input-canary";
    private static final String OUTPUT = "synthetic-output-canary";
    private static final String MESSAGE = """
        {"id":"msg_synthetic","type":"message","role":"assistant",
         "model":"test-model","content":[{"type":"text","text":"synthetic-output-canary"}],
         "stop_reason":"end_turn","stop_sequence":null,
         "usage":{"input_tokens":11,"output_tokens":7,"cache_read_input_tokens":3,"cache_creation_input_tokens":2}}
        """;

    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private HttpServer server;
    private AnthropicClient client;
    private volatile int status = 200;
    private volatile String response = MESSAGE;
    private volatile String receivedKey;
    private volatile String receivedAuthorization;
    private volatile String receivedBody;

    @BeforeEach
    void start() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/messages", exchange -> {
            receivedBody = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            receivedKey = exchange.getRequestHeaders().getFirst("x-api-key");
            receivedAuthorization = exchange.getRequestHeaders().getFirst("authorization");
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        var builder = AnthropicOkHttpClient.builder().apiKey("synthetic-client-key")
            .baseUrl("http://127.0.0.1:" + server.getAddress().getPort()).maxRetries(0);
        disableNativeTracing(builder);
        client = builder.build();
    }

    private Object params() throws Exception { return call(paramsBuilder(INPUT), "build"); }

    private TracedAnthropicClient traced() {
        return traced(TraceConfig.builder().build());
    }

    private TracedAnthropicClient traced(TraceConfig config) {
        return new TracedAnthropicClient(client, new FITracer(otel.getOpenTelemetry().getTracer("wrapper-contract"), config));
    }

    private SpanData span() {
        assertEquals(1, otel.getSpans().size(), "the wrapper must be the only span owner");
        return otel.getSpans().get(0);
    }

    private String attr(String name) { return span().getAttributes().get(AttributeKey.stringKey(name)); }
    private Long count(String name) { return span().getAttributes().get(AttributeKey.longKey(name)); }

    @Test
    void responseTextIsPlainContent() throws Exception {
        Object result = traced().createMessage(params());
        assertEquals("msg_synthetic", call(result, "id"));
        assertEquals(OUTPUT, attr(SemanticConventions.OUTPUT_VALUE));
        assertEquals("[{\"role\":\"assistant\",\"content\":\"" + OUTPUT + "\"}]", attr(SemanticConventions.LLM_OUTPUT_MESSAGES));
        assertEquals("end_turn", attr(SemanticConventions.LLM_RESPONSE_FINISH_REASON));
        assertEquals(StatusCode.OK, span().getStatus().getStatusCode());
    }

    @Test
    void noPhantomSystemMessageAndPlainInput() throws Exception {
        traced().createMessage(params());
        assertEquals("[{\"role\":\"user\",\"content\":\"" + INPUT + "\"}]", attr(SemanticConventions.LLM_INPUT_MESSAGES));
    }

    @Test
    void rawInputDoesNotExportAuthenticationHeaders() throws Exception {
        Object builder = paramsBuilder(INPUT);
        var header = builder.getClass().getMethod("putAdditionalHeader", String.class, String.class);
        header.invoke(builder, "X-Api-Key", "synthetic-request-secret");
        header.invoke(builder, "Authorization", "Bearer synthetic-auth-secret");
        header.invoke(builder, "X-Custom-Credential", "synthetic-custom-secret");
        builder.getClass().getMethod("putAdditionalQueryParam", String.class, String.class)
            .invoke(builder, "api_key", "synthetic-query-secret");
        traced().createMessage(call(builder, "build"));
        assertEquals("synthetic-request-secret", receivedKey);
        assertEquals("Bearer synthetic-auth-secret", receivedAuthorization);
        String raw = attr(SemanticConventions.RAW_INPUT);
        assertNotNull(raw);
        String exported = span().toString();
        for (String secret : List.of("synthetic-request-secret", "synthetic-auth-secret", "synthetic-custom-secret", "synthetic-client-key", "synthetic-query-secret")) {
            assertFalse(exported.contains(secret), "transport credentials must not appear in any span field");
        }
        assertTrue(raw.contains(INPUT), "safe body content should still be captured");
        assertFalse(raw.contains("additionalHeaders"));
        assertTrue(receivedBody.contains(INPUT));
    }

    @Test
    void sdkErrorsKeepOriginalType() throws Exception {
        status = 400;
        response = "{\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",\"message\":\"synthetic-error\"}}";
        Object params = params();
        RuntimeException error = assertThrows(RuntimeException.class, () -> traced().createMessage(params));
        assertEquals(BadRequestException.class.getName(), attr(SemanticConventions.ERROR_TYPE));
        assertInstanceOf(BadRequestException.class, error);
        assertEquals(StatusCode.ERROR, span().getStatus().getStatusCode());
        assertEquals(BadRequestException.class.getName(), span().getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.type")));
    }

    @Test
    void promptUsageIncludesCacheTokens() throws Exception {
        traced().createMessage(params());
        assertEquals(16L, count(SemanticConventions.LLM_TOKEN_COUNT_PROMPT));
        assertEquals(7L, count(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION));
        assertEquals(23L, count(SemanticConventions.LLM_TOKEN_COUNT_TOTAL));
        assertEquals(3L, count(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ));
        assertEquals(2L, count(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_WRITE));
    }

    @Test
    void omittedCacheCountersDoNotInventUsage() throws Exception {
        response = MESSAGE.replace(",\"cache_read_input_tokens\":3,\"cache_creation_input_tokens\":2", "");
        traced().createMessage(params());
        assertEquals(11L, count(SemanticConventions.LLM_TOKEN_COUNT_PROMPT));
        assertEquals(18L, count(SemanticConventions.LLM_TOKEN_COUNT_TOTAL));
        assertNull(count(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ));
        assertNull(count(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_WRITE));
    }

    @Test
    void configuredContentPrivacyRemainsEffective() throws Exception {
        traced(TraceConfig.builder().hideInputs(true).hideOutputs(true).build()).createMessage(params());
        assertFalse(span().toString().contains(INPUT));
        assertFalse(span().toString().contains(OUTPUT));
        assertNotNull(count(SemanticConventions.LLM_TOKEN_COUNT_PROMPT));
    }

    @Test
    void plainSystemPromptIsUnwrapped() throws Exception {
        Object builder = paramsBuilder(INPUT);
        call(builder, "system", String.class, "synthetic-system");
        traced().createMessage(call(builder, "build"));
        JsonNode messages = ObjectMappers.jsonMapper().readTree(attr(SemanticConventions.LLM_INPUT_MESSAGES));
        assertEquals(2, messages.size());
        assertEquals("system", messages.get(0).get("role").asText());
        assertEquals("synthetic-system", messages.get(0).get("content").asText());
    }

    private Object textBlock(String text) throws Exception {
        return ObjectMappers.jsonMapper().convertValue(
            java.util.Map.of("type", "text", "text", text), modelClass("TextBlockParam"));
    }

    @Test
    void structuredInputAndSystemBlocksKeepTheirTextAndRawShape() throws Exception {
        Object builder = paramsBuilder(INPUT);
        call(builder, "systemOfTextBlockParams", List.class, List.of(textBlock("first "), textBlock("second")));
        Object block = modelClass("ContentBlockParam").getMethod("ofText", modelClass("TextBlockParam"))
            .invoke(null, textBlock("block input"));
        try {
            call(builder, "addUserMessageOfContentBlockParams", List.class, List.of(block));
        } catch (NoSuchMethodException legacySdk) {
            call(builder, "addUserMessageOfBlockParams", List.class, List.of(block));
        }
        traced().createMessage(call(builder, "build"));
        JsonNode messages = ObjectMappers.jsonMapper().readTree(attr(SemanticConventions.LLM_INPUT_MESSAGES));
        assertEquals(3, messages.size());
        assertEquals("first second", messages.get(0).get("content").asText());
        assertEquals("block input", messages.get(2).get("content").asText());
        JsonNode raw = ObjectMappers.jsonMapper().readTree(attr(SemanticConventions.RAW_INPUT));
        assertEquals("first ", raw.get("system").get(0).get("text").asText());
        assertEquals("text", raw.get("messages").get(1).get("content").get(0).get("type").asText());
        assertEquals("block input", raw.get("messages").get(1).get("content").get(0).get("text").asText());
    }

    @Test
    void mixedOutputBlocksDoNotExportOptionalOrToolDebugStrings() throws Exception {
        response = MESSAGE.replace("\"content\":[{\"type\":\"text\",\"text\":\"synthetic-output-canary\"}]", """
            "content":[{"type":"text","text":"first "},
                       {"type":"tool_use","id":"tool_1","name":"lookup","input":{"query":"synthetic-tool-input"}},
                       {"type":"text","text":"second"}]
            """);
        traced().createMessage(params());
        assertEquals("first second", attr(SemanticConventions.OUTPUT_VALUE));
        JsonNode raw = ObjectMappers.jsonMapper().readTree(attr(SemanticConventions.RAW_OUTPUT));
        assertEquals("tool_use", raw.get("content").get(1).get("type").asText());
        assertEquals("synthetic-tool-input", raw.get("content").get(1).get("input").get("query").asText());
    }

    @Test
    void emptyFinishReasonDoesNotBecomeOptionalEmpty() throws Exception {
        response = MESSAGE.replace("\"stop_reason\":\"end_turn\"", "\"stop_reason\":null");
        traced().createMessage(params());
        assertNull(attr(SemanticConventions.LLM_RESPONSE_FINISH_REASON));
    }

    @Test
    void cacheCountsUseLongPrecision() throws Exception {
        response = MESSAGE.replace("\"input_tokens\":11", "\"input_tokens\":3000000000");
        traced().createMessage(params());
        assertEquals(3000000005L, count(SemanticConventions.LLM_TOKEN_COUNT_PROMPT));
        assertEquals(3000000012L, count(SemanticConventions.LLM_TOKEN_COUNT_TOTAL));
    }

    @Test
    void singleCacheCounterAndZeroRemainDistinctFromAbsent() throws Exception {
        response = MESSAGE.replace(",\"cache_creation_input_tokens\":2", "")
            .replace("\"cache_read_input_tokens\":3", "\"cache_read_input_tokens\":0");
        traced().createMessage(params());
        assertEquals(11L, count(SemanticConventions.LLM_TOKEN_COUNT_PROMPT));
        assertEquals(0L, count(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ));
        assertNull(count(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_WRITE));
    }

    @Test
    void errorPrivacyStillHidesProviderDetailsAfterUnwrapping() throws Exception {
        status = 400;
        response = "{\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",\"message\":\"synthetic-private-error\"}}";
        Object params = params();
        assertThrows(BadRequestException.class, () -> traced(TraceConfig.builder().hideErrorDetails(true).build()).createMessage(params));
        assertEquals(BadRequestException.class.getName(), attr(SemanticConventions.ERROR_TYPE));
        assertEquals(StatusCode.ERROR, span().getStatus().getStatusCode());
        assertFalse(span().toString().contains("synthetic-private-error"));
        assertNull(attr(SemanticConventions.ERROR_MESSAGE));
    }

    @AfterEach
    void close() {
        if (client != null) client.close();
        if (server != null) server.stop(0);
    }
}
