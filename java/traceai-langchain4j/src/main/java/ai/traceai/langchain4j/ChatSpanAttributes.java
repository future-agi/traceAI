package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.message.*;
import dev.langchain4j.model.output.FinishReason;
import dev.langchain4j.model.output.TokenUsage;
import io.opentelemetry.api.trace.Span;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Shared message handling uses only types present in both LangChain4j 0.35 and 1.x. */
final class ChatSpanAttributes {
    private ChatSpanAttributes() {}

    static void input(FITracer tracer, Span span, List<ChatMessage> messages) {
        if (messages == null || tracer.getConfig().isHideInputs() || tracer.getConfig().isHideInputMessages()) return;
        List<Map<String, String>> input = new ArrayList<>();
        for (ChatMessage message : messages) {
            if (message instanceof UserMessage user) {
                StringBuilder text = new StringBuilder();
                for (Content content : user.contents()) {
                    // Never stringify images, audio, or unknown content (may contain URLs/base64).
                    if (content instanceof TextContent part) text.append(part.text());
                }
                input.add(FITracer.message("user", text.toString()));
            } else if (message instanceof SystemMessage system) {
                input.add(FITracer.message("system", system.text()));
            } else if (message instanceof AiMessage ai) {
                input.add(FITracer.message("assistant", ai.text()));
            } else if (message instanceof ToolExecutionResultMessage tool) {
                input.add(FITracer.message("tool", tool.text()));
            }
        }
        tracer.setInputMessages(span, input);
    }

    static void output(FITracer tracer, Span span, AiMessage message) {
        if (message == null || tracer.getConfig().isHideOutputs()) return;
        tracer.setOutputValue(span, message.text());
        if (tracer.getConfig().isHideOutputMessages()) return;
        tracer.setOutputMessages(span, List.of(FITracer.message("assistant", message.text())));
        if (message.hasToolExecutionRequests()) {
            List<ToolExecutionRequest> requests = message.toolExecutionRequests();
            for (int i = 0; i < requests.size(); i++) {
                ToolExecutionRequest request = requests.get(i);
                String prefix = SemanticConventions.MESSAGE_TOOL_CALLS + "." + i + ".";
                span.setAttribute(prefix + SemanticConventions.TOOL_CALL_ID, request.id());
                span.setAttribute(prefix + SemanticConventions.TOOL_CALL_FUNCTION_NAME, request.name());
                span.setAttribute(prefix + SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS, request.arguments());
            }
        }
    }

    static void usage(Span span, TokenUsage usage, FinishReason finishReason) {
        if (usage != null) {
            // Providers may omit any count. Do not unbox null or invent zero usage.
            if (usage.inputTokenCount() != null)
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, usage.inputTokenCount().longValue());
            if (usage.outputTokenCount() != null)
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION, usage.outputTokenCount().longValue());
            if (usage.totalTokenCount() != null)
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, usage.totalTokenCount().longValue());
        }
        if (finishReason != null)
            span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, finishReason.toString());
    }
}
