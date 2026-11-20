package ai.traceai.googleadk;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class AdkAttributesTest {
    @SuppressWarnings("unchecked")
    private static Map<String, Object> json(String text) {
        return (Map<String, Object>) AdkAttributes.parseJson(text);
    }

    private static final String REQUEST = "{\"contents\":[{\"parts\":[{\"text\":\"weather in Kyoto?\"}],\"role\":\"user\"},"
        + "{\"parts\":[{\"functionCall\":{\"id\":\"call-1\",\"args\":{\"city\":\"Kyoto\"},\"name\":\"weather\"}}],\"role\":\"model\"},"
        + "{\"parts\":[{\"functionResponse\":{\"id\":\"call-1\",\"name\":\"weather\",\"response\":{\"city\":\"Kyoto\",\"forecast\":\"sunny\"}}}],\"role\":\"user\"}],"
        + "\"model\":\"gemini-2.0-flash\",\"config\":{\"topP\":0.9,\"maxOutputTokens\":256,\"systemInstruction\":{\"parts\":[{\"text\":\"Be brief\"}],\"role\":\"user\"},"
        + "\"tools\":[{\"functionDeclarations\":[{\"name\":\"weather\",\"description\":\"forecast\",\"parameters\":{\"type\":\"OBJECT\"}}]}]}}";
    private static final String RESPONSE = "{\"content\":{\"parts\":[{\"text\":\"It is sunny in Kyoto.\"}],\"role\":\"model\"},"
        + "\"usageMetadata\":{\"candidatesTokenCount\":7,\"promptTokenCount\":11,\"totalTokenCount\":18,\"thoughtsTokenCount\":2,\"cachedContentTokenCount\":3},\"finishReason\":\"STOP\"}";

    @Test
    void anLlmCallCarriesModelMessagesToolsAndUsage() {
        Map<String, Object> attrs = AdkAttributes.llm(json(REQUEST), json(RESPONSE));
        assertThat(attrs).containsEntry("gen_ai.span.kind", "LLM")
            .containsEntry("gen_ai.operation.name", "chat")
            .containsEntry("gen_ai.request.model", "gemini-2.0-flash")
            .containsEntry("gen_ai.provider.name", "gcp.gemini")
            .containsEntry("gen_ai.request.top_p", 0.9)
            .containsEntry("gen_ai.request.max_tokens", 256L)
            .containsEntry("gen_ai.input.messages.0.message.role", "system")
            .containsEntry("gen_ai.input.messages.0.message.content", "Be brief")
            .containsEntry("gen_ai.input.messages.1.message.role", "user")
            .containsEntry("gen_ai.input.messages.1.message.content", "weather in Kyoto?")
            .containsEntry("gen_ai.input.messages.2.message.role", "assistant")
            .containsEntry("gen_ai.input.messages.2.message.tool_calls.0.tool_call.id", "call-1")
            .containsEntry("gen_ai.input.messages.2.message.tool_calls.0.tool_call.function.name", "weather")
            .containsEntry("gen_ai.input.messages.2.message.tool_calls.0.tool_call.function.arguments", "{\"city\":\"Kyoto\"}")
            .containsEntry("gen_ai.input.messages.3.message.role", "tool")
            .containsEntry("gen_ai.input.messages.3.message.tool_call_id", "call-1")
            .containsEntry("gen_ai.input.messages.3.message.name", "weather")
            .containsEntry("gen_ai.output.messages.0.message.role", "assistant")
            .containsEntry("gen_ai.output.messages.0.message.content", "It is sunny in Kyoto.")
            .containsEntry("gen_ai.usage.input_tokens", 11L)
            .containsEntry("gen_ai.usage.output_tokens", 9L)
            .containsEntry("gen_ai.usage.total_tokens", 18L)
            .containsEntry("gen_ai.usage.output_tokens.reasoning", 2L)
            .containsEntry("gen_ai.usage.cache_read_tokens", 3L)
            .containsEntry("gen_ai.response.finish_reasons", List.of("stop"))
            .containsEntry("input.value", "weather in Kyoto?")
            .containsEntry("output.value", "It is sunny in Kyoto.");
        assertThat((String) attrs.get("gen_ai.tool.definitions.0.tool.json_schema")).contains("\"name\":\"weather\"");
    }

    @Test
    void aToolCallingResponseIsOutputAsToolCalls() {
        Map<String, Object> attrs = AdkAttributes.llm(json(REQUEST), json(
            "{\"content\":{\"parts\":[{\"functionCall\":{\"id\":\"call-2\",\"args\":{\"city\":\"Oslo\"},\"name\":\"weather\"}}],\"role\":\"model\"}}"));
        assertThat(attrs).containsEntry("gen_ai.output.messages.0.message.tool_calls.0.tool_call.id", "call-2")
            .containsEntry("output.mime_type", "application/json");
        assertThat((String) attrs.get("output.value")).contains("Oslo");
        assertThat(attrs).doesNotContainKey("gen_ai.output.messages.0.message.content");
    }

    @Test
    void anEmptyCallStillHasItsKind() {
        assertThat(AdkAttributes.llm(null, null)).containsEntry("gen_ai.span.kind", "LLM").doesNotContainKey("gen_ai.request.model");
    }

    @Test
    void aFailedModelCallNamesItsError() {
        assertThat(AdkAttributes.llmError(json("{\"errorCode\":\"429\",\"errorMessage\":\"quota\"}"))).isEqualTo("quota");
        assertThat(AdkAttributes.llmError(json(RESPONSE))).isNull();
    }

    @Test
    void aToolCallCarriesNameArgumentsAndResult() {
        Map<String, Object> attrs = AdkAttributes.tool("weather", "forecast", "FunctionTool", Map.of("city", "Kyoto"), Map.of("forecast", "sunny"), "call-1");
        assertThat(attrs).containsEntry("gen_ai.span.kind", "TOOL")
            .containsEntry("gen_ai.operation.name", "execute_tool")
            .containsEntry("gen_ai.tool.name", "weather")
            .containsEntry("gen_ai.tool.description", "forecast")
            .containsEntry("gen_ai.tool.call.id", "call-1")
            .containsEntry("gen_ai.tool.call.arguments", "{\"city\":\"Kyoto\"}")
            .containsEntry("input.value", "{\"city\":\"Kyoto\"}")
            .containsEntry("output.value", "{\"forecast\":\"sunny\"}");
    }

    @Test
    void agentInvocationAndIdentity() {
        assertThat(AdkAttributes.agent("helper", "answers")).containsEntry("gen_ai.span.kind", "AGENT").containsEntry("gen_ai.agent.name", "helper");
        assertThat(AdkAttributes.invocation()).containsEntry("gen_ai.span.kind", "CHAIN");
        assertThat(AdkAttributes.identity("s-1", "u-1")).containsEntry("session.id", "s-1").containsEntry("user.id", "u-1");
        assertThat(AdkAttributes.identity(null, null)).isEmpty();
    }

    @Test
    void providersAreNamedOnlyWhenRecognized() {
        assertThat(AdkAttributes.provider("claude-3-5-sonnet")).isEqualTo("anthropic");
        assertThat(AdkAttributes.provider("gpt-4o")).isEqualTo("openai");
        assertThat(AdkAttributes.provider("my-private-model")).isNull();
        assertThat(AdkAttributes.provider(null)).isNull();
    }

    @Test
    void aHugeListIsWalkedOnlyUpToTheBudget() {
        java.util.List<Object> huge = new java.util.ArrayList<>(java.util.Collections.nCopies(1_000_000, "x"));
        String json = org.junit.jupiter.api.Assertions.assertTimeoutPreemptively(java.time.Duration.ofSeconds(5), () -> AdkAttributes.toJson(Map.of("result", huge)));
        assertThat(json).contains("<truncated>").hasSizeLessThan(400_000);
    }

    @Test
    void anInfiniteIterableNeverHangsTheCall() {
        Iterable<String> endless = () -> new java.util.Iterator<>() {
            @Override public boolean hasNext() { return true; }
            @Override public String next() { return "x"; }
        };
        String json = org.junit.jupiter.api.Assertions.assertTimeoutPreemptively(java.time.Duration.ofSeconds(5), () -> AdkAttributes.toJson(Map.of("result", endless)));
        assertThat(json).contains("<truncated>").hasSizeLessThan(400_000);
    }

    @Test
    void enumsCharactersAndUuidsKeepTheirValues() {
        java.util.UUID id = java.util.UUID.fromString("123e4567-e89b-12d3-a456-426614174000");
        String json = AdkAttributes.toJson(Map.of("kind", java.util.concurrent.TimeUnit.SECONDS, "letter", 'q', "id", id));
        assertThat(json).contains("\"SECONDS\"").contains("\"q\"").contains("\"123e4567-e89b-12d3-a456-426614174000\"").doesNotContain("unserializable");
    }
}
