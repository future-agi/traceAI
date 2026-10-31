package ai.traceai.privacy;

import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.DelegatingSpanData;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.data.StatusData;
import io.opentelemetry.sdk.trace.export.SpanExporter;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Objects;
import java.util.Set;

/**
 * Removes exception messages, stack traces and status descriptions before handing spans to an exporter.
 *
 * <p>Use with a framework-owned OpenTelemetry SDK; this class creates no provider, processor or spans.
 * Status codes, exception types, resource, IDs and unrelated attributes are preserved. It does not
 * redact arbitrary application attributes, event names, logs or metrics. Original span data is never
 * mutated, so every outbound exporter must be wrapped if it needs this policy.
 */
public final class ErrorDetailRedactingSpanExporter implements SpanExporter {
    private final SpanExporter delegate;
    private final Set<String> additionalMessageKeys;
    private final boolean keepErrorText;

    private ErrorDetailRedactingSpanExporter(SpanExporter delegate, Set<String> additionalMessageKeys,
            boolean keepErrorText) {
        this.delegate = delegate;
        this.additionalMessageKeys = additionalMessageKeys;
        this.keepErrorText = keepErrorText;
    }

    /**
     * Wraps an exporter, or returns it unchanged if it already uses this filter.
     * @param delegate exporter that receives sanitized spans
     * @return the filtered exporter
     */
    public static SpanExporter wrap(SpanExporter delegate) {
        return wrap(delegate, Set.of(), false);
    }

    /**
     * Adds a framework's error-message keys to the standard exception policy.
     * Stack traces are always removed. When {@code keepErrorText} is true, messages and status
     * descriptions are retained. Otherwise, both span and event attributes are filtered.
     * Wrapping an existing filter never weakens its policy and does not create another export path.
     *
     * @param delegate exporter that receives sanitized spans
     * @param additionalMessageKeys framework-specific keys that can contain error text
     * @param keepErrorText explicit opt-in to error messages and status descriptions
     * @return the filtered exporter
     */
    public static SpanExporter wrap(SpanExporter delegate, Set<String> additionalMessageKeys,
            boolean keepErrorText) {
        Objects.requireNonNull(delegate, "delegate");
        Set<String> keys = Set.copyOf(additionalMessageKeys);
        if (delegate instanceof ErrorDetailRedactingSpanExporter existing
                && existing.additionalMessageKeys.equals(keys) && existing.keepErrorText == keepErrorText) {
            return delegate;
        }
        return new ErrorDetailRedactingSpanExporter(delegate, keys, keepErrorText);
    }

    @Override
    public CompletableResultCode export(Collection<SpanData> spans) {
        List<SpanData> sanitized = new ArrayList<>(spans.size());
        for (SpanData span : spans) {
            sanitized.add(redact(span));
        }
        return delegate.export(sanitized);
    }

    @Override
    public CompletableResultCode flush() {
        return delegate.flush();
    }

    @Override
    public CompletableResultCode shutdown() {
        return delegate.shutdown();
    }

    private Attributes redactAttributes(Attributes attributes) {
        return attributes.toBuilder().removeIf(key -> key.getKey().equals("exception.stacktrace")
                || (!keepErrorText && (key.getKey().equals("exception.message")
                || additionalMessageKeys.contains(key.getKey())))).build();
    }

    private SpanData redact(SpanData span) {
        Attributes attributes = redactAttributes(span.getAttributes());
        List<EventData> events = new ArrayList<>(span.getEvents().size());
        boolean changed = !attributes.equals(span.getAttributes())
                || (!keepErrorText && !span.getStatus().getDescription().isEmpty());
        for (EventData event : span.getEvents()) {
            Attributes filtered = redactAttributes(event.getAttributes());
            if (filtered.equals(event.getAttributes())) {
                events.add(event);
            } else {
                changed = true;
                int removed = event.getAttributes().size() - filtered.size();
                events.add(EventData.create(event.getEpochNanos(), event.getName(), filtered,
                        event.getTotalAttributeCount() - removed));
            }
        }
        if (!changed) {
            return span;
        }
        List<EventData> immutableEvents = List.copyOf(events);
        StatusData status = keepErrorText ? span.getStatus() : StatusData.create(span.getStatus().getStatusCode(), "");
        return new DelegatingSpanData(span) {
            @Override
            public Attributes getAttributes() {
                return attributes;
            }

            @Override
            public int getTotalAttributeCount() {
                return span.getTotalAttributeCount() - (span.getAttributes().size() - attributes.size());
            }

            @Override
            public List<EventData> getEvents() {
                return immutableEvents;
            }

            @Override
            public StatusData getStatus() {
                return status;
            }
        };
    }
}
