package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import com.google.gson.JsonArray;
import com.google.gson.JsonParser;
import java.time.Duration;
import java.util.List;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatOptions;
import static org.junit.jupiter.api.Assertions.*;

@Timeout(30)
class StructuredMessageTest {
    private static final String TOOL_REPLY = """
        {"id":"tool-id","created":1,"object":"chat.completion","model":"synthetic-model","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call-next","type":"function","function":{"name":"weather","arguments":"{\\"city\\":\\"next-city\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}
        """;
    private static final String TOOL_STREAM = """
        data: {"id":"tool-id","created":1,"object":"chat.completion.chunk","model":"synthetic-model","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-next","type":"function","function":{"name":"weather","arguments":"{\\"city\\":"}}]},"finish_reason":null}]}

        data: {"id":"tool-id","created":1,"object":"chat.completion.chunk","model":"synthetic-model","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"next-city\\"}"}}]},"finish_reason":null}]}

        data: {"id":"tool-id","created":1,"object":"chat.completion.chunk","model":"synthetic-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

        data: [DONE]

        """;

    @ParameterizedTest
    @CsvSource({"false,false,false", "false,true,false", "false,false,true", "false,true,true",
        "true,false,false", "true,true,false", "true,false,true", "true,true,true"})
    void capturesToolRequestsWithoutExecutingThemAndPreservesHistory(boolean streaming, boolean inputs,
            boolean outputs) throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter(inputs, outputs))) {
            if (streaming) f.replies.add(new NativeSpringAITest.Reply(TOOL_STREAM, "text/event-stream", 200));
            else f.reply(TOOL_REPLY);
            var options = OpenAiChatOptions.builder();
            // Spring AI 2 returns tool requests to callers; Spring AI 1 needs its internal loop disabled.
            try { options.getClass().getMethod("internalToolExecutionEnabled", Boolean.class).invoke(options, false); }
            catch (NoSuchMethodException spring2) { }
            var priorCall = AssistantMessage.builder().content("").toolCalls(
                List.of(new AssistantMessage.ToolCall("call-prior", "function", "weather", "{\"city\":\"prior-city\"}"))).build();
            var toolResults = ToolResponseMessage.builder().responses(List.of(
                new ToolResponseMessage.ToolResponse("call-prior", "weather", "prior-result"),
                new ToolResponseMessage.ToolResponse("call-other", "weather", "other-result"))).build();
            var prompt = new Prompt(List.of(new SystemMessage("system-marker"), new UserMessage("user-marker"),
                priorCall, toolResults), options.build());
            var model = Models.chat(f.url(), f.registry);
            ChatResponse response;
            if (streaming) {
                var chunks = model.stream(prompt).collectList().block(Duration.ofSeconds(10));
                assertNotNull(chunks);
                response = chunks.stream().filter(ChatResponse::hasToolCalls).findFirst().orElseThrow();
            } else response = model.call(prompt);
            assertEquals("call-next", response.getResult().getOutput().getToolCalls().get(0).id());
            assertEquals("{\"city\":\"next-city\"}", response.getResult().getOutput().getToolCalls().get(0).arguments());
            assertEquals("prior-result", toolResults.getResponses().get(0).responseData());
            assertSame(priorCall, prompt.getInstructions().get(2));
            var span = f.onlySpan();
            assertEquals(1, f.aiSpans().size(), "A tool request is not a tool execution or a second model span");
            var input = NativeSpringAITest.attr(span, "gen_ai.input.messages");
            var output = NativeSpringAITest.attr(span, "gen_ai.output.messages");
            if (inputs) {
                JsonArray messages = JsonParser.parseString(input).getAsJsonArray();
                assertEquals(5, messages.size());
                assertEquals("system", messages.get(0).getAsJsonObject().get("role").getAsString());
                assertEquals("user", messages.get(1).getAsJsonObject().get("role").getAsString());
                var call = messages.get(2).getAsJsonObject().getAsJsonArray("tool_calls").get(0).getAsJsonObject();
                assertEquals("call-prior", call.get("id").getAsString());
                assertEquals("{\"city\":\"prior-city\"}", call.getAsJsonObject("function").get("arguments").getAsString());
                for (int i = 3; i < 5; i++) {
                    var message = messages.get(i).getAsJsonObject();
                    assertEquals("tool", message.get("role").getAsString());
                    assertEquals(toolResults.getResponses().get(i - 3).id(), message.get("tool_call_id").getAsString());
                    assertEquals(toolResults.getResponses().get(i - 3).responseData(), message.get("content").getAsString());
                }
            } else {
                assertNull(input);
                assertFalse(span.getAttributes().toString().contains("prior-city"));
                assertFalse(span.getAttributes().toString().contains("prior-result"));
                assertFalse(span.getAttributes().toString().contains("system-marker"));
            }
            if (outputs) {
                var message = JsonParser.parseString(output).getAsJsonArray().get(0).getAsJsonObject();
                assertEquals("assistant", message.get("role").getAsString());
                var call = message.getAsJsonArray("tool_calls").get(0).getAsJsonObject();
                assertEquals("call-next", call.get("id").getAsString());
                assertEquals("weather", call.getAsJsonObject("function").get("name").getAsString());
                assertEquals("{\"city\":\"next-city\"}", call.getAsJsonObject("function").get("arguments").getAsString());
            } else {
                assertNull(output);
                assertFalse(span.getAttributes().toString().contains("call-next"));
                assertFalse(span.getAttributes().toString().contains("next-city"));
            }
        }
    }
}
