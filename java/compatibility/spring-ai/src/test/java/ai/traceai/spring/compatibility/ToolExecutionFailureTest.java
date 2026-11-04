package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.SpringAIToolExecutionExceptionProcessor;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationHandler;
import io.micrometer.observation.ObservationRegistry;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.model.tool.DefaultToolCallingManager;
import org.springframework.ai.model.tool.ToolCallingManager;
import org.springframework.ai.tool.execution.DefaultToolExecutionExceptionProcessor;
import org.springframework.ai.tool.execution.ToolExecutionException;
import org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor;
import org.springframework.ai.tool.observation.ToolCallingObservationContext;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import java.util.concurrent.atomic.AtomicReference;
import static org.assertj.core.api.Assertions.assertThat;
import io.opentelemetry.api.trace.StatusCode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.tool.function.FunctionToolCallback;
import java.util.concurrent.atomic.AtomicInteger;
import static ai.traceai.spring.compatibility.NativeSpringAITest.attr;
import static org.junit.jupiter.api.Assertions.*;

@Timeout(30)
class ToolExecutionFailureTest {
    static final String SENSITIVE_ERROR = "private-tool-failure-6721";
    static final String TOOL_REPLY = """
        {"id":"tool-id","created":1,"object":"chat.completion","model":"synthetic-model","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call1","type":"function","function":{"name":"weather","arguments":"{\\"city\\":\\"private-tool-city\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}
        """;
    public record ToolInput(String city) {}

    @ParameterizedTest @ValueSource(booleans = {false, true})
    void rethrowPolicyPreservesExceptionAndOneNativeToolError(boolean unwrap) throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.reply(TOOL_REPLY);
            var original = new IllegalStateException(SENSITIVE_ERROR);
            var received = new AtomicReference<ToolExecutionException>();
            var calls = new AtomicInteger();
            ToolExecutionExceptionProcessor policy = exception -> {
                received.set(exception);
                calls.incrementAndGet();
                if (unwrap) throw (RuntimeException) exception.getCause();
                return DefaultToolExecutionExceptionProcessor.builder().alwaysThrow(true).build().process(exception);
            };
            var manager = DefaultToolCallingManager.builder().observationRegistry(f.registry)
                .toolExecutionExceptionProcessor(new SpringAIToolExecutionExceptionProcessor(f.registry, policy)).build();
            var thrown = assertThrows(RuntimeException.class,
                () -> Models.toolChat(f.url(), f.registry, failingPrompt(original), manager));
            assertSame(unwrap ? original : received.get(), thrown);
            assertSame(original, received.get().getCause());
            assertEquals(1, calls.get());
            assertEquals(1, f.requests.size(), "rethrow must not trigger a recovery model request");
            var tools = f.aiSpans().stream().filter(s -> "TOOL".equals(attr(s, "fi.span.kind"))).toList();
            assertEquals(1, tools.size());
            var tool = tools.get(0);
            assertEquals(StatusCode.ERROR, tool.getStatus().getStatusCode());
            assertEquals(IllegalStateException.class.getName(), attr(tool, "error.type"));
            assertEquals(1, tool.getEvents().size(), "the framework rethrow notification must not duplicate the original failure");
            assertEquals(1, tool.getEvents().get(0).getAttributes().size());
            assertTrue(tool.getStatus().getDescription().isEmpty());
            assertFalse(f.aiSpans().toString().contains(SENSITIVE_ERROR));
        }
    }

    @ParameterizedTest @ValueSource(booleans = {false, true})
    void bootDecoratesExistingDefaultPolicyWithoutOverridingItsThrowProperty(boolean alwaysThrow) throws Exception {
        verifyBootPolicy(alwaysThrow, false);
    }

    @Test void bootPreservesCustomRecoveryPolicyDespiteThrowProperty() throws Exception {
        verifyBootPolicy(true, true);
    }

    private static void verifyBootPolicy(boolean alwaysThrow, boolean customPolicy) throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.reply(TOOL_REPLY);
            f.reply(NativeSpringAITest.CHAT);
            var original = new IllegalStateException(SENSITIVE_ERROR);
            var calls = new AtomicInteger();
            var runner = new ApplicationContextRunner().withUserConfiguration(DocumentedBootSetupTest.Application.class)
                .withPropertyValues("spring.ai.tools.throw-exception-on-error=" + alwaysThrow,
                    "management.otlp.tracing.export.enabled=false",
                    "management.opentelemetry.tracing.export.otlp.enabled=false");
            if (customPolicy) runner = runner.withBean("customToolPolicy", ToolExecutionExceptionProcessor.class,
                () -> ex -> { calls.incrementAndGet(); assertSame(original, ex.getCause()); return "custom-recovery-result"; });
            runner.run(context -> {
                assertThat(context).hasNotFailed().hasSingleBean(ToolExecutionExceptionProcessor.class)
                    .hasSingleBean(ToolCallingManager.class);
                assertThat(context.getBean(ToolExecutionExceptionProcessor.class))
                    .isInstanceOf(SpringAIToolExecutionExceptionProcessor.class);
                var registry = context.getBean(ObservationRegistry.class);
                var toolStopped = new AtomicReference<Observation.Context>();
                registry.observationConfig().observationHandler(new ObservationHandler<Observation.Context>() {
                    @Override public boolean supportsContext(Observation.Context value) {
                        return value instanceof ToolCallingObservationContext;
                    }
                    @Override public void onStop(Observation.Context value) { toolStopped.set(value); }
                });
                var manager = context.getBean(ToolCallingManager.class);
                if (alwaysThrow && !customPolicy) {
                    var thrown = assertThrows(ToolExecutionException.class,
                        () -> Models.toolChat(f.url(), registry, failingPrompt(original), manager));
                    assertSame(original, thrown.getCause());
                    assertEquals(1, f.requests.size());
                } else {
                    assertEquals(NativeSpringAITest.OUTPUT,
                        Models.toolChat(f.url(), registry, failingPrompt(original), manager).getResult().getOutput().getText());
                    assertSame(original, toolStopped.get().getError());
                    assertEquals(2, f.requests.size());
                    assertTrue(f.requests.stream().anyMatch(body -> body.contains(
                        customPolicy ? "custom-recovery-result" : SENSITIVE_ERROR)));
                }
                assertNotNull(toolStopped.get());
                assertNotNull(toolStopped.get().getError());
                assertEquals(customPolicy ? 1 : 0, calls.get());
            });
        }
    }

    static Prompt failingPrompt(RuntimeException failure) {
        var callback = FunctionToolCallback.builder("weather", (ToolInput input) -> { throw failure; })
            .description("Synthetic failing tool").inputType(ToolInput.class).build();
        return new Prompt("tool input", OpenAiChatOptions.builder().toolCallbacks(callback).build());
    }

    @Test void handledToolFailureMustNotExportSuccessfulToolSpan() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.reply(TOOL_REPLY);
            f.reply(NativeSpringAITest.CHAT);
            var invoked = new AtomicInteger();
            var original = new IllegalStateException(SENSITIVE_ERROR);
            var callback = FunctionToolCallback.builder("weather", (ToolInput input) -> {
                invoked.incrementAndGet();
                throw original;
            }).description("Synthetic failing tool").inputType(ToolInput.class).build();
            var options = OpenAiChatOptions.builder().toolCallbacks(callback).build();
            var parent = f.sdk.get("application").spanBuilder("tool-workflow").startSpan();
            try (var ignored = parent.makeCurrent()) {
                assertEquals(NativeSpringAITest.OUTPUT,
                    Models.toolChat(f.url(), f.registry, new Prompt("tool input", options)).getResult().getOutput().getText());
            } finally { parent.end(); }
            assertEquals(1, invoked.get());
            assertEquals(2, f.requests.size(), "the existing processor still lets the model recover");
            assertTrue(f.requests.stream().anyMatch(body -> body.contains(SENSITIVE_ERROR)),
                "telemetry privacy must not change the processor's tool-result message");
            assertEquals(3, f.aiSpans().size(), "two model requests and one native tool span");
            var tools = f.aiSpans().stream().filter(s -> "TOOL".equals(attr(s, "fi.span.kind"))).toList();
            assertEquals(1, tools.size());
            var tool = tools.get(0);
            assertEquals(StatusCode.ERROR, tool.getStatus().getStatusCode());
            assertEquals(IllegalStateException.class.getName(), attr(tool, "error.type"));
            assertEquals(1, tool.getEvents().size());
            assertTrue(tool.getStatus().getDescription().isEmpty());
            assertEquals(1, tool.getEvents().get(0).getAttributes().size());
            assertEquals(parent.getSpanContext().getTraceId(), tool.getTraceId());
            assertTrue(f.spans().stream().anyMatch(s -> s.getSpanId().equals(tool.getParentSpanId())));
            for (var span : f.aiSpans()) {
                if ("LLM".equals(attr(span, "fi.span.kind"))) assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
                assertFalse(span.toString().contains(SENSITIVE_ERROR));
                assertFalse(span.toString().contains("private-tool-city"));
                assertFalse(span.toString().contains("synthetic-key"));
            }
            assertEquals(SENSITIVE_ERROR, original.getMessage());
        }
    }
}
