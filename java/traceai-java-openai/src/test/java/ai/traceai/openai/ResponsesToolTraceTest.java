package ai.traceai.openai;

import ai.traceai.ContextAttributes;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import java.nio.charset.StandardCharsets;
import static ai.traceai.openai.TracingHttpClientTest.attr;
import static org.assertj.core.api.Assertions.assertThat;

class ResponsesToolTraceTest {
    @ParameterizedTest
    @CsvSource({"false,false", "false,true", "true,false", "true,true"})
    void failedMcpToolsRetainErrorAttributionWithoutLeakingHiddenDetails(boolean stream, boolean hideErrors) {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var trace = new OpenAITrace(new FITracer(provider.get("tools"), TraceConfig.builder().hideErrorDetails(hideErrors).build()), "responses");
            var response = JsonParser.parseString(OpenAILoopback.RESPONSE).getAsJsonObject();
            response.getAsJsonArray("output").add(JsonParser.parseString("{\"type\":\"mcp_call\",\"id\":\"mcp-error\",\"name\":\"lookup\","
                + "\"arguments\":\"{}\",\"server_label\":\"test\",\"error\":\"private-tool-error\"}"));
            if (stream) trace.event("{\"type\":\"response.completed\",\"response\":" + response + "}");
            else assertThat(trace.response(response.toString().getBytes(StandardCharsets.UTF_8))).isTrue();
            trace.end(true);
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
            var tool = exporter.getFinishedSpanItems().get(0);
            assertThat(tool.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(attr(tool, "error.type")).isEqualTo("openai.tool_error");
            assertThat(attr(tool, "openai.tool.status")).isEqualTo("failed");
            assertThat(tool.toString()).doesNotContain("private-tool-error");
            // A recovered server tool failure does not turn a completed LLM response into a failure.
            assertThat(exporter.getFinishedSpanItems().get(1).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            if (hideErrors) assertThat(exporter.getFinishedSpanItems().toString()).doesNotContain("private-tool-error");
            else assertThat(attr(exporter.getFinishedSpanItems().get(1), "fi.raw_output")).contains("private-tool-error");
        }
    }

    @Test void markersAreBoundedDeduplicatedAndIgnoreClientSideCallsAndPendingItems() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var trace = new OpenAITrace(new FITracer(provider.get("tools")), "responses");
            var response = JsonParser.parseString(OpenAILoopback.RESPONSE).getAsJsonObject();
            var output = new JsonArray();
            output.add("malformed item");
            output.add(JsonParser.parseString("{\"type\":\"function_call\",\"id\":\"client-tool\",\"name\":\"lookup\",\"arguments\":\"{}\"}"));
            output.add(JsonParser.parseString("{\"type\":\"web_search_call\",\"id\":\"pending\",\"status\":\"in_progress\"}"));
            for (int i = 0; i < 70; i++) {
                var tool = new JsonObject();
                tool.addProperty("type", "web_search_call"); tool.addProperty("id", "ws-" + i); tool.addProperty("status", "completed");
                output.add(tool);
                if (i == 0) output.add(tool.deepCopy());
            }
            response.add("output", output);
            String event = "{\"type\":\"response.completed\",\"response\":" + response + "}";
            trace.event(event);
            trace.event(event);
            trace.end(true);
            var spans = exporter.getFinishedSpanItems();
            assertThat(spans).hasSize(65);
            assertThat(spans.subList(0, 64)).allSatisfy(s -> {
                assertThat(attr(s, "gen_ai.span.kind")).isEqualTo("tool");
                assertThat(s.getStartEpochNanos()).isEqualTo(s.getEndEpochNanos());
                assertThat(s.getParentSpanId()).isEqualTo(trace.span.getSpanContext().getSpanId());
            });
            assertThat(spans.subList(0, 64).stream().map(s -> attr(s, "gen_ai.tool.call.id"))).doesNotHaveDuplicates();
            assertThat(spans.get(64).getAttributes().get(AttributeKey.booleanKey("traceai.tool.markers.truncated"))).isTrue();
        }
    }

    @Test void itemEventsWaitForTerminalResponseAndNothingIsRecordedAfterClose() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var trace = new OpenAITrace(new FITracer(provider.get("tools")), "responses");
            String item = "{\"type\":\"web_search_call\",\"id\":\"ws-test\",\"status\":\"completed\"}";
            String response = "{\"status\":\"completed\",\"output\":[" + item + "]}";
            trace.event("{\"type\":\"response.output_item.done\",\"item\":" + item + "}");
            trace.event("{\"type\":\"response.in_progress\",\"response\":" + response + "}");
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            trace.event("{\"type\":\"response.completed\",\"response\":" + response + "}");
            trace.end(true);
            trace.event("{\"type\":\"response.completed\",\"response\":" + response.replace("ws-test", "ws-late") + "}");
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
        }
    }

    @Test void incompleteResponsesStillAttributeCompletedToolsAndUseCallerIdentitySnapshot() throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            OpenAITrace trace;
            try (var user = ContextAttributes.usingUser("caller-user")) {
                trace = new OpenAITrace(new FITracer(provider.get("tools")), "responses");
            }
            try (var user = ContextAttributes.usingUser("worker-user")) {
                trace.event("{\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\",\"output\":["
                    + "{\"type\":\"file_search_call\",\"id\":\"fs-test\",\"status\":\"completed\"}]}}");
                trace.end(true);
            }
            assertThat(exporter.getFinishedSpanItems()).hasSize(2);
            var tool = exporter.getFinishedSpanItems().get(0);
            assertThat(attr(tool, "user.id")).isEqualTo("caller-user");
            assertThat(tool.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(exporter.getFinishedSpanItems().get(1).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(exporter.getFinishedSpanItems().toString()).doesNotContain("worker-user");
        }
    }
}
