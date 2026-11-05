package ai.traceai.openai;

import ai.traceai.ContextAttributes;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.openai.models.responses.Response;
import com.openai.models.responses.ResponseCreateParams;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import java.util.List;
import java.util.stream.Stream;
import static ai.traceai.openai.TracingHttpClientTest.attr;
import static org.assertj.core.api.Assertions.assertThat;

/** Real SDK and HTTP boundary contracts for terminal Responses metadata. */
class ModernResponsesTelemetryTest {
    static Stream<Arguments> terminalCases() {
        return Stream.of("incomplete", "failed").flatMap(status -> Stream.of(false, true).flatMap(stream ->
            Stream.of("none", "all", "messages", "errors").map(hiding -> Arguments.of(status, stream, hiding))));
    }

    @ParameterizedTest @MethodSource("terminalCases")
    void unsuccessfulResponsesRetainBillableUsage(String status, boolean stream, String hiding) throws Exception {
        String details = status.equals("incomplete") ? "\"incomplete_details\":{\"reason\":\"max_output_tokens\"}"
            : "\"error\":{\"code\":\"server_error\",\"message\":\"private-api-error\"}";
        String response = OpenAILoopback.RESPONSE.replaceFirst("\"status\":\"completed\"",
            "\"status\":\"" + status + "\"," + details);
        var spans = call(response, status, stream, hiding, null);
        assertThat(spans).hasSize(1);
        var span = spans.get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(attr(span, "error.type")).isEqualTo(status.equals("incomplete")
            ? "openai.response_incomplete" : "openai.api_error");
        assertThat(attr(span, "gen_ai.response.model")).isEqualTo("served-model");
        assertThat(attr(span, "gen_ai.response.id")).isEqualTo("resp-test");
        assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens"))).isEqualTo(10);
        assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.output_tokens"))).isEqualTo(5);
        assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(15);
        assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens.cache_read"))).isEqualTo(6);
        assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.output_tokens.reasoning"))).isEqualTo(2);
        assertThat(span.toString()).doesNotContain("private-api-error", "private-answer", "synthetic-key");
        if (hiding.equals("all") || hiding.equals("messages")) assertThat(span.toString()).doesNotContain("private-question");
    }

    static Stream<Arguments> toolCases() {
        return Stream.of("web_search_call", "file_search_call", "code_interpreter_call", "mcp_call")
            .flatMap(type -> Stream.of(false, true).flatMap(stream -> Stream.of("none", "all", "messages", "input", "output", "errors")
                .map(hiding -> Arguments.of(type, stream, hiding))));
    }

    @ParameterizedTest @MethodSource("toolCases")
    void serverToolsAreZeroDurationChildrenWithPrivacy(String type, boolean stream, String hiding) throws Exception {
        String item = tool(type);
        String response = OpenAILoopback.RESPONSE.replace("\"output\":[", "\"output\":[" + item + ",");
        var spans = call(response, "completed", stream, hiding, type);
        assertThat(spans).hasSize(2);
        var llm = spans.stream().filter(s -> "llm".equals(attr(s, "gen_ai.span.kind"))).findFirst().orElseThrow();
        var tool = spans.stream().filter(s -> "tool".equals(attr(s, "gen_ai.span.kind"))).findFirst().orElseThrow();
        assertThat(tool.getParentSpanId()).isEqualTo(llm.getSpanId());
        assertThat(tool.getTraceId()).isEqualTo(llm.getTraceId());
        assertThat(tool.getStartEpochNanos()).isEqualTo(tool.getEndEpochNanos());
        assertThat(tool.getStartEpochNanos()).isBetween(llm.getStartEpochNanos(), llm.getEndEpochNanos());
        assertThat(attr(tool, "gen_ai.tool.type")).isEqualTo(type);
        assertThat(attr(tool, "gen_ai.tool.call.id")).isEqualTo("tool-test");
        assertThat(attr(tool, "gen_ai.tool.name")).isEqualTo(type.equals("mcp_call") ? "lookup" : type.substring(0, type.length() - "_call".length()));
        assertThat(attr(tool, "openai.tool.status")).isEqualTo("completed");
        assertThat(attr(tool, "gen_ai.operation.name")).isEqualTo("execute_tool");
        assertThat(attr(tool, "user.id")).isEqualTo("caller-user");
        assertThat(tool.getAttributes().get(AttributeKey.booleanKey("traceai.tool.marker"))).isTrue();
        assertThat(tool.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        // Markers contain only attribution, never queries, code, arguments, results or errors.
        assertThat(tool.toString()).doesNotContain("private-tool-input", "private-tool-output", "private-answer");
        if (List.of("all", "messages", "input").contains(hiding)) {
            assertThat(spans.toString()).doesNotContain("private-question", "private-tool-input");
        }
        if (List.of("all", "messages", "output").contains(hiding)) {
            assertThat(spans.toString()).doesNotContain("private-answer", "private-tool-output", "private-tool-input");
        }
    }

    private static String tool(String type) {
        String fields;
        switch (type) {
            case "web_search_call": fields = "\"action\":{\"type\":\"search\",\"query\":\"private-tool-input\"}"; break;
            case "file_search_call": fields = "\"queries\":[\"private-tool-input\"],\"results\":[{\"file_id\":\"f-test\",\"filename\":\"test.txt\",\"score\":1,\"text\":\"private-tool-output\",\"attributes\":{}}]"; break;
            case "code_interpreter_call": fields = "\"container_id\":\"c-test\",\"code\":\"private-tool-input\",\"outputs\":[{\"type\":\"logs\",\"logs\":\"private-tool-output\"}]"; break;
            case "mcp_call": fields = "\"name\":\"lookup\",\"server_label\":\"server-test\",\"arguments\":\"private-tool-input\",\"output\":\"private-tool-output\""; break;
            default: throw new AssertionError(type);
        }
        String status = type.equals("mcp_call") ? "" : "\"status\":\"completed\",";
        return "{\"type\":\"" + type + "\",\"id\":\"tool-test\"," + status + fields + "}";
    }

    private static List<SpanData> call(String response, String status, boolean stream, String hiding, String toolType) throws Exception {
        String payload = stream ? "event: response." + status + "\ndata: {\"type\":\"response." + status
            + "\",\"sequence_number\":1,\"response\":" + response + "}\n\n" : response;
        var config = TraceConfig.builder().hideInputs(hiding.equals("all") || hiding.equals("input"))
            .hideOutputs(hiding.equals("all") || hiding.equals("output"))
            .hideInputMessages(hiding.equals("messages")).hideOutputMessages(hiding.equals("messages"))
            .hideErrorDetails(hiding.equals("errors")).build();
        var exporter = InMemorySpanExporter.create();
        try (var server = new OpenAILoopback(stream ? "text/event-stream" : "application/json", payload);
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(TracingHttpClient.wrap(
                OkHttpClient.builder().build(), new FITracer(provider.get("responses"), config)))
                .apiKey("synthetic-key").baseUrl(server.url()).maxRetries(0).build());
            var parent = provider.get("responses").spanBuilder("application").startSpan();
            try (AutoCloseable close = client::close; var scope = parent.makeCurrent(); var user = ContextAttributes.usingUser("caller-user")) {
                var params = ResponseCreateParams.builder().model("requested-model").input("private-question").build();
                Response result;
                if (stream) {
                    try (var events = client.responses().createStreaming(params)) {
                        var values = events.stream().toList();
                        assertThat(values).hasSize(1);
                        result = status.equals("completed") ? values.get(0).completed().orElseThrow().response()
                            : status.equals("incomplete") ? values.get(0).incomplete().orElseThrow().response()
                            : values.get(0).failed().orElseThrow().response();
                    }
                } else result = client.responses().create(params);
                assertThat(result.usage().orElseThrow().totalTokens()).isEqualTo(15);
                if (toolType != null) {
                    var item = result.output().get(0);
                    switch (toolType) {
                        case "web_search_call": assertThat(item.webSearchCall()).isPresent(); break;
                        case "file_search_call": assertThat(item.fileSearchCall()).isPresent(); break;
                        case "code_interpreter_call": assertThat(item.codeInterpreterCall()).isPresent(); break;
                        case "mcp_call": assertThat(item.mcpCall()).isPresent(); break;
                        default: throw new AssertionError(toolType);
                    }
                }
                var spans = exporter.getFinishedSpanItems();
                var llm = spans.stream().filter(s -> "llm".equals(attr(s, "gen_ai.span.kind"))).findFirst().orElseThrow();
                assertThat(llm.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
                return spans;
            } finally { parent.end(); }
        }
    }
}
