package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.openai.core.ObjectMappers;
import com.openai.models.ChatCompletionCreateParams;
import com.openai.models.CompletionUsage;
import com.openai.models.EmbeddingCreateParams;
import io.opentelemetry.api.trace.Span;
import java.util.LinkedHashMap;
import java.util.Map;

/** Body-only telemetry for the legacy facade. Never serialize SDK request containers. */
final class LegacyOpenAITrace {
    private LegacyOpenAITrace() {}

    static void input(FITracer tracer, Span span, ChatCompletionCreateParams params, boolean streaming) {
        if (tracer.getConfig().isHideInputs() || tracer.getConfig().isHideInputMessages()) return;
        // An allowlist, not a filtered params object: transport headers, query parameters and
        // arbitrary additional body properties must never enter raw-input serialization.
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", params.model().toString());
        body.put("messages", params.messages());
        body.put("stream", streaming);
        params.temperature().ifPresent(value -> body.put("temperature", value));
        params.topP().ifPresent(value -> body.put("top_p", value));
        params.maxTokens().ifPresent(value -> body.put("max_tokens", value));
        params.maxCompletionTokens().ifPresent(value -> body.put("max_completion_tokens", value));
        params.frequencyPenalty().ifPresent(value -> body.put("frequency_penalty", value));
        params.presencePenalty().ifPresent(value -> body.put("presence_penalty", value));
        params.seed().ifPresent(value -> body.put("seed", value));
        params.stop().ifPresent(value -> body.put("stop", value));
        params.n().ifPresent(value -> body.put("n", value));
        params.tools().ifPresent(value -> body.put("tools", value));
        params.toolChoice().ifPresent(value -> body.put("tool_choice", value));
        params.parallelToolCalls().ifPresent(value -> body.put("parallel_tool_calls", value));
        params.responseFormat().ifPresent(value -> body.put("response_format", value));
        params.streamOptions().ifPresent(value -> body.put("stream_options", value));
        rawInput(tracer, span, body);
    }

    static void input(FITracer tracer, Span span, EmbeddingCreateParams params) {
        if (tracer.getConfig().isHideInputs() || tracer.getConfig().isHideInputMessages()) return;
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", params.model().toString());
        body.put("input", params.input());
        params.dimensions().ifPresent(value -> body.put("dimensions", value));
        params.encodingFormat().ifPresent(value -> body.put("encoding_format", value));
        rawInput(tracer, span, body);
    }

    private static void rawInput(FITracer tracer, Span span, Map<String, Object> body) {
        try {
            // The SDK mapper understands its body unions; Gson reflects SDK internals instead.
            tracer.setRawInput(span, ObjectMappers.jsonMapper().writeValueAsString(body));
        } catch (JsonProcessingException | RuntimeException ignored) {
            // Never fall back to params.toString(), nor log a payload-bearing serialization error.
            span.setAttribute("traceai.capture.skipped", true);
        }
    }

    static void usage(FITracer tracer, Span span, CompletionUsage usage) {
        tracer.setTokenCounts(span, usage.promptTokens(), usage.completionTokens(), usage.totalTokens());
        try {
            usage.promptTokensDetails().ifPresent(details -> details.cachedTokens().ifPresent(value ->
                span.setAttribute(SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ, value)));
            usage.completionTokensDetails().ifPresent(details -> details.reasoningTokens().ifPresent(value ->
                span.setAttribute(SemanticConventions.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING, value)));
        } catch (RuntimeException ignored) {
            // Optional detail parsing must not turn a usable SDK response into a call failure.
            span.setAttribute("traceai.capture.skipped", true);
        }
    }
}
