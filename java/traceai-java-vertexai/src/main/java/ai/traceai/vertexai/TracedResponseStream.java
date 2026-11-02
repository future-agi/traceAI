package ai.traceai.vertexai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.google.cloud.vertexai.api.Candidate;
import com.google.cloud.vertexai.api.GenerateContentResponse;
import com.google.cloud.vertexai.generativeai.ResponseStream;
import com.google.cloud.vertexai.generativeai.ResponseStreamIteratorWithHistory;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.Collections;
import java.util.Iterator;
import java.util.NoSuchElementException;
import java.util.stream.Stream;

/** Keeps the SDK's single-consumer/history semantics and adds a close hook. */
final class TracedResponseStream extends ResponseStream<GenerateContentResponse> {
    private final Capture capture;

    private TracedResponseStream(Capture capture) {
        super(new ResponseStreamIteratorWithHistory<GenerateContentResponse>(capture));
        this.capture = capture;
    }

    static ResponseStream<GenerateContentResponse> wrap(
            ResponseStream<GenerateContentResponse> source, Span span, FITracer tracer) {
        return new TracedResponseStream(new Capture(source.iterator(), span, tracer));
    }

    @Override
    public Stream<GenerateContentResponse> stream() {
        // The SDK has no cancellation API. Closing ends tracing, not the RPC.
        return super.stream().onClose(() -> capture.finish(false, null));
    }

    private static final class Capture implements Iterator<GenerateContentResponse> {
        // Bound telemetry separately from the SDK's own response history.
        private static final int LIMIT = 32000;
        private final Iterator<GenerateContentResponse> source;
        private final Span span;
        private final FITracer tracer;
        private final StringBuilder text = new StringBuilder();
        private final JsonArray raw = new JsonArray();
        private int rawLength = 2;
        private boolean rawFull;
        private boolean textFull;
        private boolean ended;

        Capture(Iterator<GenerateContentResponse> source, Span span, FITracer tracer) {
            this.source = source;
            this.span = span;
            this.tracer = tracer;
        }

        @Override
        public boolean hasNext() {
            if (ended) return false;
            try (Scope ignored = span.makeCurrent()) {
                boolean more = source.hasNext();
                if (!more) finish(true, null);
                return more;
            } catch (RuntimeException e) {
                finish(false, e);
                throw e;
            }
        }

        @Override
        public GenerateContentResponse next() {
            if (ended) throw new NoSuchElementException();
            try (Scope ignored = span.makeCurrent()) {
                // Also handle callers who use next() without hasNext().
                if (!hasNext()) throw new NoSuchElementException();
                GenerateContentResponse response = source.next();
                capture(response);
                return response;
            } catch (RuntimeException e) {
                finish(false, e);
                throw e;
            }
        }

        private void capture(GenerateContentResponse response) {
            if (response.getCandidatesCount() > 0) {
                Candidate candidate = response.getCandidates(0);
                if (!tracer.getConfig().isHideOutputs() && candidate.hasContent() && !textFull) {
                    String delta = TracedGenerativeModel.extractContentText(candidate.getContent());
                    int end = Math.min(delta.length(), LIMIT - text.length());
                    if (end > 0 && end < delta.length()
                            && Character.isHighSurrogate(delta.charAt(end - 1))
                            && Character.isLowSurrogate(delta.charAt(end))) {
                        end--;
                    }
                    text.append(delta, 0, end);
                    // Once any text is omitted, later chunks must not fill the gap.
                    textFull = end < delta.length() || text.length() == LIMIT;
                }
                if (candidate.getFinishReasonValue() != 0) {
                    span.setAttribute(AttributeKey.stringArrayKey(SemanticConventions.LLM_RESPONSE_FINISH_REASON),
                        Collections.singletonList(candidate.getFinishReason().name()));
                }
            }
            if (response.hasUsageMetadata()) {
                GenerateContentResponse.UsageMetadata usage = response.getUsageMetadata();
                tracer.setTokenCounts(span, usage.getPromptTokenCount(),
                    usage.getCandidatesTokenCount(), usage.getTotalTokenCount());
            }
            if (!tracer.getConfig().isHideOutputs() && !rawFull) {
                JsonElement json = TracedGenerativeModel.protobufJson(response);
                if (json != null) {
                    int length = json.toString().length() + (raw.size() == 0 ? 0 : 1);
                    if (rawLength + length <= LIMIT) {
                        raw.add(json);
                        rawLength += length;
                    } else {
                        rawFull = true;
                        span.setAttribute("fi.stream.raw_output_truncated", true);
                    }
                }
            }
        }

        void finish(boolean complete, RuntimeException error) {
            if (ended) return;
            ended = true;
            try {
                if (!tracer.getConfig().isHideOutputs()) {
                    tracer.setOutputValue(span, text.toString());
                    tracer.setOutputMessages(span,
                        Collections.singletonList(FITracer.message("model", text.toString())));
                    tracer.setRawOutput(span, raw);
                }
                if (error != null) {
                    tracer.setError(span, error);
                } else if (complete) {
                    span.setStatus(StatusCode.OK);
                } else {
                    span.setAttribute("fi.stream.incomplete", true);
                }
            } finally {
                span.end();
            }
        }
    }
}
