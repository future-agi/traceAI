package ai.traceai.googlegenai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.google.genai.types.Content;
import com.google.genai.types.EmbedContentResponse;
import com.google.genai.types.GenerateContentConfig;
import com.google.genai.types.GenerateContentResponse;
import com.google.gson.JsonArray;
import com.google.gson.JsonParser;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/** One logical SDK operation, independent of threads and HTTP implementation. */
final class GenAiOperation {
    private static final int STREAM_CAPTURE_LIMIT = 32768;
    final FITracer tracer;
    final Span span;
    final Context context;
    private final AtomicBoolean ended = new AtomicBoolean();
    private final StringBuilder text = new StringBuilder();
    private final JsonArray chunks = new JsonArray();
    private int chunkCharacters;

    GenAiOperation(FITracer tracer, String name, String model, FISpanKind kind,
                   Object input, GenerateContentConfig config) {
        this.tracer = tracer;
        span = tracer.startSpan("Google GenAI " + name, kind);
        context = Context.current().with(span);
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "google-genai");
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
        span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
        span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);
        if (kind == FISpanKind.EMBEDDING) {
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, model);
        }
        // Only content is serialized: HttpOptions can contain authorization headers.
        safely(() -> {
            if (tracer.getConfig().isHideInputs()) return;
            List<Map<String, String>> messages = new ArrayList<>();
            if (config != null) {
                config.systemInstruction().ifPresent(value ->
                    messages.add(FITracer.message("system", value.text())));
            }
            if (input instanceof String) {
                tracer.setInputValue(span, (String) input);
                messages.add(FITracer.message("user", (String) input));
            } else if (input instanceof List<?>) {
                JsonArray raw = new JsonArray();
                for (Object item : (List<?>) input) {
                    if (item instanceof Content) {
                        Content content = (Content) item;
                        messages.add(FITracer.message(content.role().orElse("user"), content.text()));
                        raw.add(JsonParser.parseString(content.toJson()));
                    } else if (item instanceof String) {
                        messages.add(FITracer.message("user", (String) item));
                        raw.add((String) item);
                    }
                }
                tracer.setRawInput(span, raw);
            }
            tracer.setInputMessages(span, messages);
        });
    }

    void response(GenerateContentResponse response, boolean streaming) {
        if (response == null || ended.get()) return;
        safely(() -> response.modelVersion().ifPresent(model ->
            span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, model)));
        safely(() -> response.usageMetadata().ifPresent(usage -> {
            long prompt = usage.promptTokenCount().orElse(0);
            long completion = (long) usage.candidatesTokenCount().orElse(0) + usage.thoughtsTokenCount().orElse(0);
            long total = usage.totalTokenCount().map(Integer::longValue).orElse(prompt + completion);
            // Streaming usage is a cumulative snapshot, not a per-chunk delta.
            tracer.setTokenCounts(span, prompt, completion, total);
        }));
        safely(() -> response.candidates().ifPresent(candidates -> {
            if (!candidates.isEmpty()) candidates.get(0).finishReason().ifPresent(reason ->
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, reason.toString()));
        }));
        if (tracer.getConfig().isHideOutputs()) return;
        safely(() -> {
            String value = response.text();
            if (streaming) {
                if (value != null && text.length() < STREAM_CAPTURE_LIMIT) {
                    text.append(value, 0, Math.min(value.length(), STREAM_CAPTURE_LIMIT - text.length()));
                }
            } else {
                tracer.setOutputValue(span, value);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", value)));
            }
        });
        safely(() -> {
            var raw = JsonParser.parseString(response.toJson()).getAsJsonObject();
            // Newer SDKs attach transport headers; those are not model output.
            raw.remove("sdkHttpResponse");
            // Automatic-call history can contain prompts even when inputs are hidden.
            raw.remove("automaticFunctionCallingHistory");
            String json = raw.toString();
            if (!streaming) {
                tracer.setRawOutput(span, raw);
            } else if (json.length() <= STREAM_CAPTURE_LIMIT - chunkCharacters) {
                chunks.add(raw);
                chunkCharacters += json.length();
            } else {
                span.setAttribute("google.genai.stream.capture_truncated", true);
            }
        });
    }

    void embeddings(EmbedContentResponse response) {
        safely(() -> response.embeddings().ifPresent(values -> {
            span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, values.size());
            if (!values.isEmpty()) values.get(0).values().ifPresent(vector ->
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, vector.size()));
        }));
        // Embedding vectors can reveal input content. Do not export them by default.
    }

    void finish(Throwable error, boolean streaming, boolean cancelled) {
        if (!ended.compareAndSet(false, true)) return;
        try {
            if (streaming && !tracer.getConfig().isHideOutputs()) {
                safely(() -> {
                    tracer.setOutputValue(span, text.toString());
                    tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", text.toString())));
                    tracer.setRawOutput(span, chunks);
                });
            }
            if (error != null) {
                safely(() -> tracer.setError(span, error));
                span.setStatus(StatusCode.ERROR);
            } else if (cancelled) {
                span.setAttribute("google.genai.cancelled", true);
            } else {
                span.setStatus(StatusCode.OK);
            }
        } finally {
            span.end();
        }
    }

    static void safely(Runnable capture) {
        try {
            capture.run();
        } catch (RuntimeException error) {
            // Fail open without logging payload-bearing exception messages or stacks.
            org.slf4j.LoggerFactory.getLogger(GenAiOperation.class).debug(
                "Skipping Google GenAI telemetry extraction ({})", error.getClass().getName());
        }
    }
}
