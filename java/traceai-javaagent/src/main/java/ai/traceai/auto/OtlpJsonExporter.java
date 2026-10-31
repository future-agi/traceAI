package ai.traceai.auto;

import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;

/** OTLP/HTTP JSON avoids shipping a second copy of the application's HTTP client. */
final class OtlpJsonExporter implements SpanExporter {
    private final AutoConfig config;
    private volatile HttpClient client;
    volatile String lastExport = "not_tested (no startup network request)";
    OtlpJsonExporter(AutoConfig config) {
        this.config = config;
        config.credentialValues.forEach(KnownSecrets.PROCESS::register);
    }
    @Override public CompletableResultCode export(Collection<SpanData> spans) {
        if (spans.isEmpty()) return CompletableResultCode.ofSuccess();
        try {
            List<Object> resources = new ArrayList<>();
            for (SpanData span : spans) resources.add(serialize(span));
            if (client == null) client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
            String base = config.get("FI_BASE_URL").replaceAll("/+$", "");
            HttpRequest request = HttpRequest.newBuilder(URI.create(base + "/tracer/v1/traces")).timeout(Duration.ofSeconds(2))
                .header("Content-Type", "application/json").header("X-Api-Key", config.get("FI_API_KEY"))
                .header("X-Secret-Key", config.get("FI_SECRET_KEY")).header("fi-project-name", projectHeader(config.get("FI_PROJECT_NAME")))
                .POST(HttpRequest.BodyPublishers.ofString(WireCore.dump(Map.of("resourceSpans", resources)))).build();
            int status;
            try (AutoCloseable ignored = TraceAIAuto.suppress()) { status = client.send(request, HttpResponse.BodyHandlers.discarding()).statusCode(); }
            if (status >= 200 && status < 300) { lastExport = "accepted"; return CompletableResultCode.ofSuccess(); }
            lastExport = "rejected (HTTP " + status + ")";
        } catch (Throwable ignored) { lastExport = "unreachable"; }
        TraceAIAuto.warn("Collector export failed; application requests are unaffected");
        return CompletableResultCode.ofFailure();
    }
    Map<String, Object> serialize(SpanData span) {
        try { return sanitized(span); }
        catch (Throwable ignored) {
            // Never drop neighbouring spans, and never fall back to unsanitized content.
            Map<String, Object> identity = new LinkedHashMap<>();
            identity.put("traceId", span.getTraceId()); identity.put("spanId", span.getSpanId());
            identity.put("parentSpanId", span.getParentSpanId()); identity.put("name", "<redacted>");
            identity.put("kind", span.getKind().ordinal() + 1);
            identity.put("startTimeUnixNano", Long.toString(span.getStartEpochNanos())); identity.put("endTimeUnixNano", Long.toString(span.getEndEpochNanos()));
            return Map.of("resource", Map.of("attributes", List.of()), "scopeSpans", List.of(Map.of("scope", Map.of("name", "traceai-auto"), "spans", List.of(identity))));
        }
    }
    private Map<String, Object> sanitized(SpanData span) {
        WirePrivacy privacy = ExportPrivacy.privacy(config);
        String operation = Optional.ofNullable(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("gen_ai.operation.name"))).orElse("chat");
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("traceId", span.getTraceId()); data.put("spanId", span.getSpanId());
        data.put("traceState", traceState(span.getSpanContext(), privacy));
        if (!span.getParentSpanId().equals("0000000000000000")) data.put("parentSpanId", span.getParentSpanId());
        data.put("name", privacy.text(span.getName())); data.put("kind", span.getKind().ordinal() + 1);
        data.put("startTimeUnixNano", String.valueOf(span.getStartEpochNanos())); data.put("endTimeUnixNano", String.valueOf(span.getEndEpochNanos()));
        Attributes attrs = ExportPrivacy.attributes(span.getAttributes(), privacy, config, false, operation);
        data.put("attributes", attributes(attrs)); data.put("droppedAttributesCount", Math.max(0, span.getTotalAttributeCount() - attrs.size()));
        data.put("status", Map.of("code", span.getStatus().getStatusCode().ordinal(), "message", TraceAIAuto.hidesContent(config) ? "" : privacy.text(span.getStatus().getDescription())));
        List<Object> events = new ArrayList<>(), links = new ArrayList<>();
        for (var event : span.getEvents()) {
            Attributes clean = ExportPrivacy.attributes(event.getAttributes(), privacy, config, true, operation);
            events.add(Map.of("timeUnixNano", String.valueOf(event.getEpochNanos()), "name", TraceAIAuto.hidesContent(config) ? "<redacted>" : privacy.text(event.getName()), "attributes", attributes(clean),
                "droppedAttributesCount", Math.max(0, event.getTotalAttributeCount() - clean.size())));
        }
        for (var link : span.getLinks()) {
            Attributes clean = ExportPrivacy.attributes(link.getAttributes(), privacy, config, false, operation);
            links.add(Map.of("traceId", link.getSpanContext().getTraceId(), "spanId", link.getSpanContext().getSpanId(), "traceState", traceState(link.getSpanContext(), privacy),
                "attributes", attributes(clean), "droppedAttributesCount", Math.max(0, link.getTotalAttributeCount() - clean.size())));
        }
        data.put("events", events); data.put("links", links);
        data.put("droppedEventsCount", Math.max(0, span.getTotalRecordedEvents() - events.size()));
        data.put("droppedLinksCount", Math.max(0, span.getTotalRecordedLinks() - links.size()));
        var info = span.getInstrumentationScopeInfo();
        Map<String, Object> scope = new LinkedHashMap<>(); scope.put("name", privacy.text(info.getName()));
        if (info.getVersion() != null) scope.put("version", privacy.text(info.getVersion()));
        scope.put("attributes", attributes(ExportPrivacy.attributes(info.getAttributes(), privacy, config, false, operation)));
        Map<String, Object> scopeSpans = new LinkedHashMap<>(); scopeSpans.put("scope", scope); scopeSpans.put("spans", List.of(data));
        if (info.getSchemaUrl() != null) scopeSpans.put("schemaUrl", privacy.text(info.getSchemaUrl()));
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("resource", Map.of("attributes", attributes(ExportPrivacy.attributes(span.getResource().getAttributes(), privacy, config, false, operation))));
        if (span.getResource().getSchemaUrl() != null) result.put("schemaUrl", privacy.text(span.getResource().getSchemaUrl()));
        result.put("scopeSpans", List.of(scopeSpans));
        if (privacy.failed) throw new IllegalStateException("Privacy snapshot failed");
        return result;
    }
    private static String traceState(io.opentelemetry.api.trace.SpanContext context, WirePrivacy privacy) {
        // Keep exact scalar boundaries and names until catalog recognition has run. Framing
        // first (vendor=base64) would disguise media and credential/audio-named values.
        Map<String, Object> entries = new LinkedHashMap<>(context.getTraceState().asMap());
        WirePrivacy.Scope scope = privacy.scope(entries, "", WirePrivacy.Scope.EMPTY);
        List<String> clean = new ArrayList<>();
        entries.forEach((key, value) -> clean.add(privacy.text(key) + "=" + privacy.clean(key, value, scope, true)));
        return String.join(",", clean);
    }
    static String projectHeader(String name) {
        // Keep existing ASCII routing values unchanged; UTF-8 percent encoding is HTTP-header safe.
        return name.chars().allMatch(c -> c >= 32 && c < 127) ? name : java.net.URLEncoder.encode(name, java.nio.charset.StandardCharsets.UTF_8).replace("+", "%20");
    }
    static List<Object> attributes(Attributes attributes) {
        List<Object> list = new ArrayList<>(); attributes.forEach((k, v) -> list.add(Map.of("key", k.getKey(), "value", any(v)))); return list;
    }
    static Map<String, Object> any(Object value) {
        if (value instanceof Boolean) return Map.of("boolValue", value);
        if (value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte) return Map.of("intValue", String.valueOf(value));
        // OTLP JSON uses strings for the three non-finite IEEE-754 values.
        if (value instanceof Double number) return Map.of("doubleValue", Double.isFinite(number) ? value : number.toString());
        if (value instanceof Float number) return Map.of("doubleValue", Float.isFinite(number) ? value : number.toString());
        if (value instanceof List<?> list) return Map.of("arrayValue", Map.of("values", list.stream().map(OtlpJsonExporter::any).toList()));
        return Map.of("stringValue", value instanceof String ? value : value == null ? "null" : value.getClass().getName());
    }
    @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
    @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
}
