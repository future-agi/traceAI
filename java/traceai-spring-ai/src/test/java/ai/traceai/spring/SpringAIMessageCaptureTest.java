package ai.traceai.spring;

import com.google.gson.JsonParser;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.chat.prompt.Prompt;
import static org.assertj.core.api.Assertions.assertThat;

class SpringAIMessageCaptureTest {
    @ParameterizedTest @ValueSource(strings = {"text", "\"\\\n\u0001<&>", "😀"})
    void largeEscapedValuesRemainBoundedValidJsonAndLeaveOriginalUntouched(String fragment) {
        String large = fragment.repeat(100_000);
        var message = AssistantMessage.builder().content(large).properties(Map.of("private-metadata", "do-not-copy"))
            .toolCalls(List.of(new AssistantMessage.ToolCall("call-1", "function", "weather", large))).build();
        var response = new ChatResponse(List.of(new Generation(message)));
        var context = ChatModelObservationContext.builder().prompt(new Prompt(large)).provider("synthetic").build();
        context.setResponse(response);
        new SpringAIObservationFilter(true, true).map(context);
        for (String key : List.of("gen_ai.input.messages", "gen_ai.output.messages")) {
            String json = context.getHighCardinalityKeyValue(key).getValue();
            assertThat(json.length()).isLessThanOrEqualTo(32_000);
            var messages = JsonParser.parseString(json).getAsJsonArray();
            assertThat(messages.size()).isEqualTo(1);
            assertThat(messages.get(0).getAsJsonObject().get("role").getAsString()).isNotEmpty();
            assertThat(json).doesNotContain("private-metadata", "do-not-copy");
        }
        var captured = JsonParser.parseString(context.getHighCardinalityKeyValue("gen_ai.output.messages")
            .getValue()).getAsJsonArray().get(0).getAsJsonObject().getAsJsonArray("tool_calls")
            .get(0).getAsJsonObject();
        assertThat(captured.get("id").getAsString()).isEqualTo("call-1");
        String arguments = captured.getAsJsonObject("function").get("arguments").getAsString();
        assertThat(arguments).isNotEmpty().isNotEqualTo(large);
        assertThat(large).startsWith(arguments);
        assertThat(Character.isHighSurrogate(arguments.charAt(arguments.length() - 1))).isFalse();
        assertThat(context.getRequest().getContents()).isEqualTo(large);
        assertThat(context.getResponse()).isSameAs(response);
        assertThat(message.getText()).isEqualTo(large);
        assertThat(message.getToolCalls().get(0).arguments()).isEqualTo(large);
    }

    @Test void manyMessagesToolCallsAndToolResultsCannotGrowThePayloadWithoutBound() {
        var capture = new SpringAIMessageCapture();
        int captured = 0;
        for (int i = 0; i < 100_000; i++) {
            if (!capture.add(new UserMessage("message-" + i))) break;
            captured++;
        }
        assertThat(captured).isBetween(1, 1000);
        assertBoundedJson(capture);
        var tools = new SpringAIMessageCapture();
        tools.add(AssistantMessage.builder().content("").toolCalls(Collections.nCopies(100_000,
            new AssistantMessage.ToolCall("call", "function", "weather", "{}"))).build());
        assertBoundedJson(tools);
        var results = new SpringAIMessageCapture();
        results.add(ToolResponseMessage.builder().responses(Collections.nCopies(100_000,
            new ToolResponseMessage.ToolResponse("call", "weather", "result"))).build());
        assertBoundedJson(results);
    }

    @Test void multipleOutputGenerationsRetainRolesAndContentWithoutMutatingResponse() {
        var context = ChatModelObservationContext.builder().prompt(new Prompt(List.of(
            new SystemMessage("same"), new UserMessage("same")))).provider("synthetic").build();
        var response = new ChatResponse(List.of(new Generation(new AssistantMessage("first")),
            new Generation(new AssistantMessage("second"))));
        context.setResponse(response);
        new SpringAIObservationFilter(true, true).map(context);
        var inputs = JsonParser.parseString(context.getHighCardinalityKeyValue("gen_ai.input.messages").getValue()).getAsJsonArray();
        var outputs = JsonParser.parseString(context.getHighCardinalityKeyValue("gen_ai.output.messages").getValue()).getAsJsonArray();
        assertThat(inputs.get(0).getAsJsonObject().get("role").getAsString()).isEqualTo("system");
        assertThat(inputs.get(1).getAsJsonObject().get("role").getAsString()).isEqualTo("user");
        assertThat(outputs.size()).isEqualTo(2);
        assertThat(outputs.get(1).getAsJsonObject().get("content").getAsString()).isEqualTo("second");
        assertThat(context.getResponse()).isSameAs(response);
    }

    private static void assertBoundedJson(SpringAIMessageCapture capture) {
        assertThat(capture.json().length()).isLessThanOrEqualTo(32_000);
        assertThat(JsonParser.parseString(capture.json()).getAsJsonArray().isEmpty()).isFalse();
    }
}
