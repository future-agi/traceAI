package ai.traceai.auto;

import java.util.*;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.*;

/** A7 admission before SDK eviction. Sorting and atomic groups are independent of insertion order. */
final class AttributeProjection {
    static final String DROPPED = "traceai.capture.dropped_content_count";
    static final AtomicLong ZERO_CAPACITY = new AtomicLong();
    static final AtomicLong PROTECTED_OVERFLOW = new AtomicLong();
    private static final Pattern MESSAGE = Pattern.compile("gen_ai\\.(input|output)\\.messages\\.(\\d+)(?:\\.|$)");
    private static final Pattern TOOL = Pattern.compile("gen_ai\\.tool\\.definitions\\.(\\d+)(?:\\.|$)");
    private static final Set<String> DEGRADATION = Set.of(DROPPED, "traceai.wire.truncated", "traceai.wire.incomplete", "traceai.wire.body_unavailable", "traceai.wire.undecoded_encoding");
    private static final Set<String> OUTCOME = Set.of("error.type", "http.response.status_code", "gen_ai.response.finish_reasons");
    private static final Set<String> MODEL = Set.of("gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.model", "gen_ai.operation.name");
    private static final Set<String> CONTENT = Set.of("gen_ai.system_instructions", "gen_ai.audio.transcript", "gen_ai.request.parameters", "llm.invocation_parameters",
        "fi.raw_input", "fi.raw_output", "cohere.preamble", "embedding.input_text", "gen_ai.reranker.query");
    static int family(String key) {
        if (DEGRADATION.contains(key)) return 0;
        if (key.equals("gen_ai.span.kind") || key.equals("gen_ai.response.id") || key.equals("project_name") || key.startsWith("project_")) return 1;
        if (OUTCOME.contains(key)) return 2;
        if (MODEL.contains(key)) return 3;
        if (key.startsWith("gen_ai.usage.")) return 4;
        if (key.equals("traceai.capture.layer") || key.equals("traceai.wire.endpoint") || key.startsWith("server.") || key.equals("http.request.method")
            || key.equals("gen_ai.request.stream") || key.startsWith("gen_ai.response.time_to_first_")) return 5;
        return 6;
    }
    static boolean content(String key) {
        return key.startsWith("input.") || key.startsWith("output.") || key.startsWith("gen_ai.input.") || key.startsWith("gen_ai.output.")
            || key.startsWith("gen_ai.tool.") || CONTENT.contains(key) || key.startsWith("message.tool_calls");
    }
    static Map<String, Object> sanitize(Map<String, Object> source, AutoConfig config, WireCore.Match match) {
        WirePrivacy privacy = ExportPrivacy.privacy(config); Map<String, Object> clean = new TreeMap<>();
        try {
            WirePrivacy.Scope scope = privacy.scope(source, "", WirePrivacy.Scope.EMPTY);
            for (var e : source.entrySet()) if (!TraceAIAuto.hidden(e.getKey(), match, config)) clean.put(privacy.text(e.getKey()), privacy.clean(e.getKey(), e.getValue(), scope, true));
            if (!privacy.failed) return clean;
        } catch (Throwable ignored) { }
        // Sanitizer work limits must not let a very large history erase already-known usage
        // or outcome. No content or arbitrary metadata can escape this fallback.
        clean.clear(); long dropped = 0;
        for (var e : source.entrySet()) {
            if (family(e.getKey()) < 6) clean.put(e.getKey(), privacy.clean(e.getKey(), e.getValue()));
            else if (content(e.getKey()) && !TraceAIAuto.hidden(e.getKey(), match, config)) dropped++;
        }
        if (dropped > 0) clean.put(DROPPED, sum(count(clean.get(DROPPED)), dropped));
        return clean;
    }
    private record Group(String id, int priority, long index, SortedMap<String, Object> entries, boolean content) { }
    static Map<String, Object> select(Map<String, Object> source, int limit, String operation) {
        limit = Math.max(0, limit);
        SortedMap<String, Object> candidates = new TreeMap<>(source);
        List<Group> groups = groups(candidates, operation);
        long contentCount = groups.stream().filter(Group::content).mapToLong(g -> g.entries.size()).sum();
        long previous = count(candidates.get(DROPPED));
        if (previous == 0) candidates.remove(DROPPED);
        Map<String, Object> selected = selectOnce(candidates, groups, limit);
        long omitted = contentCount - emittedContent(groups, selected);
        if (omitted > 0) {
            candidates.put(DROPPED, sum(previous, omitted));
            selected = selectOnce(candidates, groups, limit);
            omitted = contentCount - emittedContent(groups, selected);
            if (selected.containsKey(DROPPED)) selected.put(DROPPED, sum(previous, omitted));
        }
        if (limit == 0 && !source.isEmpty()) increment(ZERO_CAPACITY);
        if (candidates.keySet().stream().filter(k -> family(k) < 6).count() > limit) increment(PROTECTED_OVERFLOW);
        return selected;
    }
    private static void increment(AtomicLong counter) { counter.updateAndGet(v -> v == Long.MAX_VALUE ? v : v + 1); }
    private static long count(Object value) { return value instanceof Number n ? Math.max(0, n.longValue()) : 0; }
    private static long sum(long a, long b) { return a > Long.MAX_VALUE - b ? Long.MAX_VALUE : a + b; }
    private static long emittedContent(List<Group> groups, Map<String, Object> selected) {
        return groups.stream().filter(Group::content).flatMap(g -> g.entries.keySet().stream()).filter(selected::containsKey).count();
    }
    private static Map<String, Object> selectOnce(SortedMap<String, Object> candidates, List<Group> groups, int limit) {
        Map<String, Object> selected = new LinkedHashMap<>();
        List<String> protectedKeys = candidates.keySet().stream().filter(k -> family(k) < 6).sorted(Comparator.comparingInt(AttributeProjection::family).thenComparing(k -> k)).toList();
        for (String key : protectedKeys) { if (selected.size() == limit) return selected; selected.put(key, candidates.get(key)); }
        boolean outputDropped = false, inputDropped = false;
        for (Group group : groups) {
            boolean input = group.priority >= 2 && group.priority <= 5;
            if (input && outputDropped || group.priority == 2 && inputDropped || group.priority == 5 && inputDropped) continue;
            if (group.entries.size() <= limit - selected.size()) selected.putAll(group.entries);
            else {
                if (group.priority <= 1) outputDropped = true;
                if (group.priority == 2) inputDropped = true;
            }
        }
        return selected;
    }
    private static List<Group> groups(SortedMap<String, Object> source, String operation) {
        Map<String, Group> groups = new HashMap<>();
        // MIME/type companions are grouped with all candidate payload descendants at their
        // exact indexed parent. Sanitization has already seen the full pre-projection context.
        Map<String, PrivacyCatalog.Hint> hints = PrivacyCatalog.INSTANCE.indexHints(source, "", Map.of());
        List<String> parents = hints.keySet().stream().sorted(Comparator.comparingInt(String::length).reversed().thenComparing(s -> s)).toList();
        for (var entry : source.entrySet()) {
            String key = entry.getKey(); if (family(key) < 6) continue;
            String id = key; int priority = 6; long index = 0; boolean isContent = content(key);
            Matcher message = MESSAGE.matcher(key), tool = TOOL.matcher(key);
            if (message.find()) {
                boolean input = message.group(1).equals("input"); id = message.group().replaceAll("\\.$", ""); priority = input ? 2 : 0;
                try { index = Long.parseLong(message.group(2)); } catch (NumberFormatException ignored) { index = Long.MAX_VALUE; }
                if (input) index = -index;
            } else if (key.startsWith("output.") || key.equals("fi.raw_output") || key.startsWith("gen_ai.output.") || key.startsWith("message.tool_calls")) { id = "output.aggregate"; priority = 1; }
            else if (key.equals("gen_ai.audio.transcript") || key.equals("gen_ai.audio.mime_type") && source.containsKey("gen_ai.audio.transcript")) { id = "audio.transcript"; priority = "text_to_speech".equals(operation) ? 3 : 1; isContent = true; }
            else if (key.startsWith("input.") || key.equals("fi.raw_input")) { id = "input.aggregate"; priority = 5; }
            else if (tool.find()) { id = tool.group().replaceAll("\\.$", ""); priority = 3; try { index = Long.parseLong(tool.group(1)); } catch (NumberFormatException ignored) { index = Long.MAX_VALUE; } }
            else if (key.equals("gen_ai.request.parameters") || key.equals("llm.invocation_parameters")) { id = "input.parameters"; priority = 4; }
            else if (isContent) priority = 3;
            if (priority == 6 || priority == 3 && id.equals(key)) {
                String canonical = String.join(".", PrivacyCatalog.path(key, true));
                for (String parent : parents) if (parent.isEmpty() || canonical.equals(parent) || canonical.startsWith(parent + ".")) {
                    id = "media:" + parent; isContent = true; break;
                }
            }
            Group existing = groups.get(id);
            if (existing == null) { existing = new Group(id, priority, index, new TreeMap<>(), isContent); groups.put(id, existing); }
            existing.entries.put(key, entry.getValue());
        }
        return groups.values().stream().sorted(Comparator.comparingInt(Group::priority).thenComparingLong(Group::index).thenComparing(Group::id)).toList();
    }
    private AttributeProjection() { }
}
