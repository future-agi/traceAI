package ai.traceai.examples.quarkus;

import io.opentelemetry.proto.common.v1.AnyValue;
import io.opentelemetry.proto.common.v1.KeyValue;
import io.opentelemetry.proto.trace.v1.ResourceSpans;
import io.opentelemetry.proto.trace.v1.ScopeSpans;
import io.opentelemetry.proto.trace.v1.Span;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Predicate;

/** What the stand-in collector received, decoded. */
final class Exported {

    record ExportedSpan(Span span, Map<String, String> resource) {
        String name() {
            return span.getName();
        }

        String spanId() {
            return HexFormat.of().formatHex(span.getSpanId().toByteArray());
        }

        String parentId() {
            return HexFormat.of().formatHex(span.getParentSpanId().toByteArray());
        }

        String traceId() {
            return HexFormat.of().formatHex(span.getTraceId().toByteArray());
        }

        Map<String, String> attributes() {
            return toMap(span.getAttributesList());
        }

        Optional<String> attribute(String key) {
            return Optional.ofNullable(attributes().get(key));
        }
    }

    private Exported() {
    }

    static List<ExportedSpan> spans() {
        List<ExportedSpan> out = new ArrayList<>();
        for (OtlpRecorder.Post post : StandIns.collector.posts()) {
            for (ResourceSpans rs : post.decode().getResourceSpansList()) {
                Map<String, String> resource = toMap(rs.getResource().getAttributesList());
                for (ScopeSpans ss : rs.getScopeSpansList()) {
                    for (Span s : ss.getSpansList()) {
                        out.add(new ExportedSpan(s, resource));
                    }
                }
            }
        }
        return out;
    }

    /** Waits until a span matching {@code done} has been received (the simple processor exports asynchronously). */
    static List<ExportedSpan> awaitSpans(Predicate<ExportedSpan> done, Duration timeout) throws InterruptedException {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (System.nanoTime() < deadline) {
            List<ExportedSpan> spans = spans();
            if (spans.stream().anyMatch(done)) {
                Thread.sleep(200); // let sibling spans of the same trace land
                return spans();
            }
            Thread.sleep(50);
        }
        throw new AssertionError("no matching span within " + timeout + ", got " + spans().stream().map(ExportedSpan::name).toList());
    }

    static final String AI_SERVICE_SPAN = "langchain4j.aiservices.Assistant.chat";

    /** Waits for the AI service span (it ends last) and returns every span of its trace. */
    static List<ExportedSpan> awaitTrace() throws InterruptedException {
        List<ExportedSpan> all = awaitSpans(s -> s.name().equals(AI_SERVICE_SPAN), Duration.ofSeconds(10));
        String traceId = all.stream().filter(s -> s.name().equals(AI_SERVICE_SPAN)).reduce((a, b) -> b).orElseThrow().traceId();
        return all.stream().filter(s -> s.traceId().equals(traceId)).toList();
    }

    static ExportedSpan only(List<ExportedSpan> spans, String name) {
        List<ExportedSpan> matching = spans.stream().filter(s -> s.name().equals(name)).toList();
        if (matching.size() != 1) {
            throw new AssertionError("expected one span " + name + ", got " + matching.size() + "\n" + tree(spans));
        }
        return matching.get(0);
    }

    static List<ExportedSpan> named(List<ExportedSpan> spans, String name) {
        return spans.stream().filter(s -> s.name().equals(name)).toList();
    }

    /** Every byte the collector received, headers included, as one string for leak scans. */
    static String everythingReceived() {
        StringBuilder all = new StringBuilder();
        for (OtlpRecorder.Post post : StandIns.collector.posts()) {
            all.append(post.path()).append('\n').append(post.headers()).append('\n')
                    .append(new String(post.body(), StandardCharsets.ISO_8859_1)).append('\n');
        }
        return all.toString();
    }

    static String tree(List<ExportedSpan> spans) {
        StringBuilder out = new StringBuilder();
        for (ExportedSpan s : spans) {
            out.append(s.traceId(), 0, 8).append(' ').append(s.spanId(), 0, 8).append(" <- ")
                    .append(s.parentId().isEmpty() ? "root    " : s.parentId().substring(0, 8)).append(' ')
                    .append(s.name()).append(" status=").append(s.span().getStatus().getCode())
                    .append(" desc='").append(s.span().getStatus().getMessage()).append("' events=")
                    .append(s.span().getEventsCount()).append(' ').append(s.attributes()).append('\n');
        }
        return out.toString();
    }

    static Map<String, String> toMap(List<KeyValue> kvs) {
        Map<String, String> m = new LinkedHashMap<>();
        for (KeyValue kv : kvs) {
            m.put(kv.getKey(), value(kv.getValue()));
        }
        return m;
    }

    private static String value(AnyValue v) {
        return switch (v.getValueCase()) {
            case STRING_VALUE -> v.getStringValue();
            case INT_VALUE -> Long.toString(v.getIntValue());
            case DOUBLE_VALUE -> Double.toString(v.getDoubleValue());
            case BOOL_VALUE -> Boolean.toString(v.getBoolValue());
            default -> v.toString();
        };
    }
}
