package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import io.micrometer.observation.ObservationRegistry;
import io.opentelemetry.api.trace.SpanKind;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatOptions;
import static org.junit.jupiter.api.Assertions.*;

class QualificationCoverageTest {
    @Test void contentOptInPreservesStructuredMessagesAndToolCallData() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter(true, true))) {
            f.reply("""
                {"id":"tool-id","created":1,"object":"chat.completion","model":"synthetic-model","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call-qualification-42","type":"function","function":{"name":"weather","arguments":"{\\"city\\":\\"city-qualification-42\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}
                """);
            var optionsBuilder = OpenAiChatOptions.builder();
            // Spring AI 1 owns an internal tool loop; Spring AI 2 returns tool requests to callers.
            try { optionsBuilder.getClass().getMethod("internalToolExecutionEnabled", Boolean.class).invoke(optionsBuilder, false); }
            catch (NoSuchMethodException absentOnSpring2) { }
            var prompt = new Prompt(List.of(new SystemMessage("same-text"), new UserMessage("same-text")), optionsBuilder.build());
            var response = Models.chat(f.url(), f.registry).call(prompt);
            assertTrue(response.hasToolCalls(), "real upstream must return the tool call unchanged");
            assertEquals("call-qualification-42", response.getResult().getOutput().getToolCalls().get(0).id());
            var span = f.onlySpan();
            String attrs = span.getAttributes().toString();
            System.out.println("QUALIFICATION tool-call span attributes=" + attrs);
            assertAll(
                () -> assertTrue(attrs.contains("call-qualification-42"), "tool call id missing from opted-in span"),
                () -> assertTrue(attrs.contains("city-qualification-42"), "tool call arguments missing from opted-in span"),
                () -> assertTrue(attrs.contains("system") && attrs.contains("user"), "structured message roles missing from opted-in span"));
        }
    }

    @Test void nativeInstrumentationNegativeControl() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.reply(NativeSpringAITest.CHAT);
            Models.chat(f.url(), ObservationRegistry.NOOP).call(new Prompt("synthetic input"));
            assertEquals(0, f.spans().size(), "instrumentation-disabled client must emit no spans");
            f.reply(NativeSpringAITest.CHAT);
            Models.chat(f.url(), f.registry).call(new Prompt("synthetic input"));
            var span = f.onlySpan();
            assertEquals("LLM", NativeSpringAITest.attr(span, "fi.span.kind"));
            assertEquals(SpanKind.INTERNAL, span.getKind(), "generic Micrometer native model span kind");
        }
    }
}
