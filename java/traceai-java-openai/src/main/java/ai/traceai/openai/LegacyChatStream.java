package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.openai.core.http.StreamResponse;
import com.openai.models.ChatCompletionChunk;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import java.util.Iterator;
import java.util.List;
import java.util.NoSuchElementException;
import java.util.Spliterator;
import java.util.Spliterators;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;

/** Single-use legacy stream; neither acquisition nor wrapping reads a chunk. */
final class LegacyChatStream implements StreamResponse<ChatCompletionChunk>, Iterable<ChatCompletionChunk> {
    private final StreamResponse<ChatCompletionChunk> delegate;
    private final Span span;
    private final FITracer tracer;
    private final Context context;
    private final AtomicBoolean acquired = new AtomicBoolean();
    private final AtomicBoolean closed = new AtomicBoolean();
    private final StringBuilder content = new StringBuilder();
    private boolean exhausted;
    private boolean failed;

    LegacyChatStream(StreamResponse<ChatCompletionChunk> delegate, Span span, FITracer tracer) {
        this.delegate = delegate;
        this.span = span;
        this.tracer = tracer;
        context = Context.current().with(span).with(OpenAITrace.LEGACY, true);
    }

    @Override public Iterator<ChatCompletionChunk> iterator() {
        if (!acquired.compareAndSet(false, true)) throw new IllegalStateException("Stream already consumed");
        final Iterator<ChatCompletionChunk> source;
        try (Scope scope = context.makeCurrent()) {
            source = delegate.stream().iterator();
        } catch (Throwable error) {
            fail(error);
            throw OpenAITrace.propagate(error);
        }
        return new Iterator<>() {
            @Override public boolean hasNext() {
                if (closed.get()) return false;
                try (Scope scope = context.makeCurrent()) {
                    if (source.hasNext()) return true;
                    exhausted = true;
                    closeUnchecked();
                    return false;
                } catch (Throwable error) {
                    fail(error);
                    throw OpenAITrace.propagate(error);
                }
            }
            @Override public ChatCompletionChunk next() {
                if (!hasNext()) throw new NoSuchElementException();
                try (Scope scope = context.makeCurrent()) {
                    ChatCompletionChunk chunk = source.next();
                    observe(chunk);
                    return chunk;
                } catch (Throwable error) {
                    fail(error);
                    throw OpenAITrace.propagate(error);
                }
            }
        };
    }

    @Override public Stream<ChatCompletionChunk> stream() {
        return StreamSupport.stream(Spliterators.spliteratorUnknownSize(iterator(), Spliterator.ORDERED), false)
            .onClose(this::closeUnchecked);
    }

    private void observe(ChatCompletionChunk chunk) {
        // Telemetry extraction must not turn a valid SDK chunk into an application error.
        try {
            span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, chunk.id());
            span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, chunk.model());
            for (ChatCompletionChunk.Choice choice : chunk.choices()) {
                choice.finishReason().ifPresent(reason ->
                    span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, reason.toString()));
                if (choice.index() == 0 && !tracer.getConfig().isHideOutputs() && !tracer.getConfig().isHideOutputMessages()) {
                    choice.delta().content().ifPresent(value -> {
                        int remaining = 32000 - content.length();
                        content.append(value, 0, Math.min(remaining, value.length()));
                        if (value.length() > remaining) span.setAttribute("traceai.capture.truncated", true);
                    });
                }
            }
            chunk.usage().ifPresent(usage -> tracer.setTokenCounts(span,
                usage.promptTokens(), usage.completionTokens(), usage.totalTokens()));
        } catch (RuntimeException ignored) { /* Preserve the SDK's chunk unchanged. */ }
    }

    private void fail(Throwable error) {
        failed = true;
        tracer.setError(span, error);
        try { close(); } catch (Exception | Error closing) { if (closing != error) error.addSuppressed(closing); }
    }

    private void closeUnchecked() {
        try { close(); }
        catch (RuntimeException error) { throw OpenAITrace.propagate(error); }
        catch (Exception error) { throw new IllegalStateException("Unable to close OpenAI stream", error); }
    }

    @Override public void close() throws Exception {
        if (!closed.compareAndSet(false, true)) return;
        try (Scope scope = context.makeCurrent()) {
            delegate.close();
            if (!tracer.getConfig().isHideOutputMessages()) {
                tracer.setOutputValue(span, content.toString());
                tracer.setOutputMessages(span, List.of(FITracer.message("assistant", content.toString())));
            }
            if (!failed && exhausted) span.setStatus(StatusCode.OK);
            if (!exhausted) span.setAttribute("traceai.response.closed_early", true);
        } catch (Exception | Error error) {
            tracer.setError(span, error);
            throw OpenAITrace.propagate(error);
        } finally {
            span.end();
        }
    }
}
