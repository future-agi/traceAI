package ai.traceai.privacy;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributesBuilder;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.common.InstrumentationScopeInfo;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.data.DelegatingSpanData;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.LinkData;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.data.StatusData;
import io.opentelemetry.sdk.trace.export.SpanExporter;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Objects;

/**
 * Replaces known credential values in span content just before an exporter sees the span.
 *
 * <p>Covered: span name, string and string-array attributes, event names and attributes, link
 * attributes and the status description. Resource and instrumentation scope (name, version, attributes) go through the same pass.
 * Original span data is never mutated, so every outbound exporter that needs this policy must be
 * wrapped; {@code TraceAI.init(TraceConfig)} wraps the exporters it creates. Exporters on an
 * OpenTelemetry SDK the application owns are not reachable by TraceAI and are only covered when the
 * application wraps them with {@link #wrap(SpanExporter)}.</p>
 *
 * <p>If the registry has failed closed, spans are dropped and the export reports failure.</p>
 */
public final class SecretReplacingSpanExporter implements SpanExporter {
    private final SpanExporter delegate;
    private final KnownSecrets secrets;

    private SecretReplacingSpanExporter(SpanExporter delegate, KnownSecrets secrets) {
        this.delegate = delegate;
        this.secrets = secrets;
    }

    /**
     * Wraps an exporter with replacement from the process-wide registry.
     * @param delegate exporter that receives replaced spans
     * @return the wrapping exporter, or {@code delegate} if it already is one
     */
    public static SpanExporter wrap(SpanExporter delegate) {
        return wrap(delegate, KnownSecrets.process());
    }

    /**
     * Wraps an exporter with replacement from the given registry.
     * @param delegate exporter that receives replaced spans
     * @param secrets the registry to read at export time
     * @return the wrapping exporter
     */
    public static SpanExporter wrap(SpanExporter delegate, KnownSecrets secrets) {
        Objects.requireNonNull(delegate, "delegate");
        Objects.requireNonNull(secrets, "secrets");
        if (delegate instanceof SecretReplacingSpanExporter
                && ((SecretReplacingSpanExporter) delegate).secrets == secrets) {
            return delegate;
        }
        return new SecretReplacingSpanExporter(delegate, secrets);
    }

    @Override
    public CompletableResultCode export(Collection<SpanData> spans) {
        List<SpanData> replaced;
        try {
            KnownSecrets.Snapshot snapshot = secrets.snapshot();
            if (snapshot.isUnavailable()) {
                snapshot.warnDropped();
                return CompletableResultCode.ofFailure();
            }
            if (snapshot.isEmpty()) {
                return delegate.export(spans);
            }
            replaced = new ArrayList<>(spans.size());
            for (SpanData span : spans) {
                replaced.add(replace(span, snapshot));
            }
        } catch (RuntimeException e) {
            return CompletableResultCode.ofFailure();
        }
        return delegate.export(replaced);
    }

    @Override
    public CompletableResultCode flush() {
        return delegate.flush();
    }

    @Override
    public CompletableResultCode shutdown() {
        return delegate.shutdown();
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static Attributes replace(Attributes attributes, KnownSecrets.Snapshot snapshot) {
        boolean[] changed = {false};
        AttributesBuilder builder = Attributes.builder();
        attributes.forEach((key, value) -> {
            Object next = value;
            if (value instanceof String) {
                next = snapshot.scrub((String) value);
            } else if (value instanceof List && key.getType() == io.opentelemetry.api.common.AttributeType.STRING_ARRAY) {
                List<String> items = new ArrayList<>(((List<String>) value).size());
                for (String item : (List<String>) value) {
                    items.add(snapshot.scrub(item));
                }
                next = items;
            }
            if (!next.equals(value)) {
                changed[0] = true;
            }
            builder.put((AttributeKey) key, next);
        });
        return changed[0] ? builder.build() : attributes;
    }

    private static SpanData replace(SpanData span, KnownSecrets.Snapshot snapshot) {
        String name = snapshot.scrub(span.getName());
        Attributes attributes = replace(span.getAttributes(), snapshot);
        StatusData status = span.getStatus();
        String description = snapshot.scrub(status.getDescription());
        StatusData newStatus = description.equals(status.getDescription())
                ? status : StatusData.create(status.getStatusCode(), description);

        List<EventData> events = new ArrayList<>(span.getEvents().size());
        for (EventData event : span.getEvents()) {
            String eventName = snapshot.scrub(event.getName());
            Attributes eventAttributes = replace(event.getAttributes(), snapshot);
            events.add(eventName.equals(event.getName()) && eventAttributes == event.getAttributes()
                    ? event
                    : EventData.create(event.getEpochNanos(), eventName, eventAttributes,
                            event.getTotalAttributeCount()));
        }
        List<LinkData> links = new ArrayList<>(span.getLinks().size());
        for (LinkData link : span.getLinks()) {
            Attributes linkAttributes = replace(link.getAttributes(), snapshot);
            links.add(linkAttributes == link.getAttributes()
                    ? link
                    : LinkData.create(link.getSpanContext(), linkAttributes, link.getTotalAttributeCount()));
        }
        Resource resource = span.getResource();
        Attributes resourceAttributes = replace(resource.getAttributes(), snapshot);
        Resource newResource = resourceAttributes == resource.getAttributes()
                ? resource : Resource.create(resourceAttributes, resource.getSchemaUrl());
        InstrumentationScopeInfo scope = span.getInstrumentationScopeInfo();
        String scopeName = snapshot.scrub(scope.getName());
        String scopeVersion = scope.getVersion() == null ? null : snapshot.scrub(scope.getVersion());
        Attributes scopeAttributes = replace(scope.getAttributes(), snapshot);
        InstrumentationScopeInfo newScope = scopeName.equals(scope.getName())
                && Objects.equals(scopeVersion, scope.getVersion()) && scopeAttributes == scope.getAttributes()
                ? scope
                : InstrumentationScopeInfo.builder(scopeName).setVersion(scopeVersion)
                        .setSchemaUrl(scope.getSchemaUrl()).setAttributes(scopeAttributes).build();
        List<EventData> immutableEvents = List.copyOf(events);
        List<LinkData> immutableLinks = List.copyOf(links);
        return new DelegatingSpanData(span) {
            @Override
            public String getName() {
                return name;
            }

            @Override
            public Attributes getAttributes() {
                return attributes;
            }

            @Override
            public Resource getResource() {
                return newResource;
            }

            @Override
            public InstrumentationScopeInfo getInstrumentationScopeInfo() {
                return newScope;
            }

            @Override
            @SuppressWarnings("deprecation")
            public io.opentelemetry.sdk.common.InstrumentationLibraryInfo getInstrumentationLibraryInfo() {
                return io.opentelemetry.sdk.common.InstrumentationLibraryInfo.create(newScope.getName(), newScope.getVersion());
            }

            @Override
            public List<EventData> getEvents() {
                return immutableEvents;
            }

            @Override
            public List<LinkData> getLinks() {
                return immutableLinks;
            }

            @Override
            public StatusData getStatus() {
                return newStatus;
            }
        };
    }
}
