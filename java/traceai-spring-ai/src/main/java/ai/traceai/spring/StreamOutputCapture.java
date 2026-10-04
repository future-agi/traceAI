package ai.traceai.spring;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.Span;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;

import java.util.Collections;

/**
 * Bounded per-subscription output capture for streamed chat responses.
 *
 * <p>Allocates no text buffer when both output switches ({@code hideOutputs} and {@code hideOutputMessages})
 * hide the content. Otherwise retains at most a 32,000 character prefix plus an overflow flag; the delivered
 * stream elements are never modified or truncated. Token usage is never aggregated from chunks.</p>
 */
final class StreamOutputCapture {

    /** Matches the OpenTelemetry attribute budget used by {@link FITracer}. */
    static final int MAX_CHARS = 32_000;
    static final String TRUNCATION_SUFFIX = "...[truncated]";

    private final FITracer tracer;
    private final StringBuilder buffer;
    private boolean overflow;

    StreamOutputCapture(FITracer tracer) {
        this.tracer = tracer;
        TraceConfig config = tracer.getConfig();
        boolean hideAll = config.isHideOutputs() && config.isHideOutputMessages();
        this.buffer = hideAll ? null : new StringBuilder();
    }

    boolean isCapturing() {
        return buffer != null;
    }

    boolean hasOverflow() {
        return overflow;
    }

    void accept(ChatResponse response) {
        if (buffer == null) {
            return;
        }
        Generation generation = response.getResult();
        if (generation == null) {
            return;
        }
        AssistantMessage output = generation.getOutput();
        if (output == null) {
            return;
        }
        String text = output.getText();
        if (text == null || text.isEmpty()) {
            return;
        }
        int remaining = MAX_CHARS - buffer.length();
        if (text.length() <= remaining) {
            buffer.append(text);
        } else {
            if (remaining > 0) {
                buffer.append(text, 0, remaining);
            }
            overflow = true;
        }
    }

    /** Applies the captured output to the span; privacy switches are enforced by {@link FITracer}. */
    void complete(Span span) {
        if (buffer == null) {
            return;
        }
        String text = overflow
            ? buffer.substring(0, MAX_CHARS - TRUNCATION_SUFFIX.length()) + TRUNCATION_SUFFIX
            : buffer.toString();
        tracer.setOutputValue(span, text);
        tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", text)));
    }
}
