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

    /** The process-wide secret registry starts and ends empty, so sizes and replacements do not depend on the tests that ran before. */
    @org.junit.jupiter.api.BeforeEach
    @org.junit.jupiter.api.AfterEach
    void knownSecretsEmptyAround() {
        ai.traceai.privacy.KnownSecretsTestReset.reset();
    }
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
        assertThat(arguments).endsWith(ai.traceai.privacy.KnownSecrets.TRUNCATION_MARKER);
        String kept = arguments.substring(0, arguments.length() - ai.traceai.privacy.KnownSecrets.TRUNCATION_MARKER.length());
        assertThat(large).startsWith(kept);
        assertThat(kept).isNotEmpty();
        assertThat(Character.isHighSurrogate(kept.charAt(kept.length() - 1))).isFalse();
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

    @Test
    void aKnownSecretStraddlingTheMessageBudgetLeavesNoPrefix() {
        String secret = "PFX-SPRING-MESSAGE-STRADDLE-SECRET";
        ai.traceai.privacy.KnownSecrets.process().register(secret);
        for (int pad = 31_700; pad <= 32_000; pad += 7) {
            String text = "a".repeat(pad) + secret + "tail".repeat(100);
            var message = AssistantMessage.builder().content(text).build();
            var context = ChatModelObservationContext.builder().prompt(new Prompt(text)).provider("synthetic").build();
            context.setResponse(new ChatResponse(List.of(new Generation(message))));
            new SpringAIObservationFilter(true, true).map(context);
            for (String key : List.of("gen_ai.input.messages", "gen_ai.output.messages")) {
                assertThat(context.getHighCardinalityKeyValue(key).getValue()).as("%s at pad %d", key, pad).doesNotContain("PFX");
            }
        }
    }

    /** Several short secrets seal a field; the sealed value must still fit, so the messages JSON parses and stays under the cap. */
    @Test
    void aSealedFieldStillFitsTheBudgetAndTheMessagesJsonParses() {
        // Eight-character secrets become the ten-character placeholder, so sealing grows the text.
        ai.traceai.privacy.KnownSecrets.process().register("SHORTSEC");
        ai.traceai.privacy.KnownSecrets.process().register("SHORTSE2");
        for (int pad = 0; pad < 40; pad += 3) {
            String text = "x".repeat(pad) + "SHORTSEC SHORTSE2 \"".repeat(2_500);
            var message = AssistantMessage.builder().content(text).build();
            var context = ChatModelObservationContext.builder().prompt(new Prompt(text)).provider("synthetic").build();
            context.setResponse(new ChatResponse(List.of(new Generation(message))));
            new SpringAIObservationFilter(true, true).map(context);
            for (String key : List.of("gen_ai.input.messages", "gen_ai.output.messages")) {
                String json = context.getHighCardinalityKeyValue(key).getValue();
                assertThat(json.length()).as("%s at pad %d", key, pad).isLessThanOrEqualTo(32_000);
                assertThat(JsonParser.parseString(json).getAsJsonArray()).as("%s at pad %d", key, pad).hasSize(1);
            }
        }
    }

    /** Sealed cut text sits inside the JSON; a secret registered after the capture is trimmed there at export. */
    @org.junit.jupiter.params.ParameterizedTest
    @ValueSource(ints = {1, 2, 40})
    void aSecretLearnedAfterTheCaptureLeavesNoPrefixInsideTheMessagesJson(int inside) {
        String late = "PFX-SPRING-LATE-" + inside + "-" + "L".repeat(70);
        String filler = "a".repeat(31_000);
        String text = filler + late + "tail".repeat(400);
        var message = AssistantMessage.builder().content(text).build();
        var context = ChatModelObservationContext.builder().prompt(new Prompt(text)).provider("synthetic").build();
        context.setResponse(new ChatResponse(List.of(new Generation(message))));
        new SpringAIObservationFilter(true, true).map(context);
        // Slide the secret so that exactly `inside` characters of it precede the cut.
        String json = context.getHighCardinalityKeyValue("gen_ai.output.messages").getValue();
        int cut = json.indexOf(ai.traceai.privacy.KnownSecrets.TRUNCATION_MARKER);
        assertThat(cut).isPositive();
        String shifted = json.substring(0, cut) + late.substring(0, inside) + json.substring(cut);
        ai.traceai.privacy.KnownSecrets.process().register(late);
        String exported = ai.traceai.privacy.KnownSecrets.process().snapshot().scrub(shifted);
        for (int k = 1; k < late.length(); k++) {
            assertThat(exported).doesNotContain(late.substring(0, k) + ai.traceai.privacy.KnownSecrets.TRUNCATION_MARKER);
        }
        assertThat(exported).contains("<redacted>" + ai.traceai.privacy.KnownSecrets.TRUNCATION_MARKER);
    }

    /** One registry snapshot serves the budget probe and the seal of a field: the capture asks for it once per field, and uses it throughout. */
    @Test
    void aFieldIsProbedAndSealedWithOneSnapshot() {
        ai.traceai.privacy.KnownSecrets registry = new ai.traceai.privacy.KnownSecrets();
        registry.register("SNAPSHOT-SECRET-1234");
        int[] calls = {0};
        var capture = new SpringAIMessageCapture(() -> { calls[0]++; return registry.snapshot(); });
        // Larger than the budget, so the content is cut and sealed with that snapshot's secrets.
        String text = ("filler SNAPSHOT-SECRET-1234 ").repeat(3_000);
        capture.add(AssistantMessage.builder().content(text).build());
        // One call for the role and one for the content: never one per probe step.
        assertThat(calls[0]).isEqualTo(2);
        String json = capture.json();
        assertThat(json).doesNotContain("SNAPSHOT-SECRET-1234").contains(ai.traceai.privacy.KnownSecrets.PLACEHOLDER);
        assertThat(json.length()).isLessThanOrEqualTo(32_000);
        assertThat(JsonParser.parseString(json).getAsJsonArray()).hasSize(1);
    }
}
