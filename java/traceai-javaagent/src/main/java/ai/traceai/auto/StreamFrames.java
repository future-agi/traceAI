package ai.traceai.auto;

import java.nio.charset.StandardCharsets;
import java.util.*;

/** Complete frames in an observed byte prefix. Byte offsets remain independent of UTF-8 decoding. */
final class StreamFrames {
    /** At most 8192 offset/time pairs per capture; no parsing or payload retention on arrival. */
    static final class Timeline {
        private final List<Arrival> arrivals = new ArrayList<>();
        boolean lost;
        private record Arrival(long end, double seconds) { }
        synchronized void add(long end, double seconds) {
            if (lost || !arrivals.isEmpty() && end <= arrivals.get(arrivals.size() - 1).end) return;
            if (arrivals.size() == 8192) { lost = true; return; }
            arrivals.add(new Arrival(end, Math.max(0, seconds)));
        }
        synchronized Double at(int end) {
            int low = 0, high = arrivals.size();
            while (low < high) { int mid = (low + high) >>> 1; if (arrivals.get(mid).end < end) low = mid + 1; else high = mid; }
            return low < arrivals.size() ? arrivals.get(low).seconds : null;
        }
    }
    record Frame(Map<String, Object> value, int end) { }
    record Decoded(WireCore.Body body, List<Frame> frames) { }
    static Decoded decode(byte[] bytes, String type) {
        if (type.equals("text/event-stream")) return lines(bytes, true);
        if (type.contains("ndjson") || type.equals("application/jsonl")) return lines(bytes, false);
        String text = new String(bytes, StandardCharsets.UTF_8);
        Object raw = WireCore.parse(text);
        int start = 0; while (start < bytes.length && whitespace(bytes[start])) start++;
        if (start < bytes.length && bytes[start] == '[') {
            List<Frame> frames = array(bytes, start + 1);
            Object json = raw != null ? raw : frames.stream().map(Frame::value).toList();
            return new Decoded(new WireCore.Body("json", json, List.of(), null), frames);
        }
        if (raw != null) {
            int end = bytes.length; while (end > 0 && whitespace(bytes[end - 1])) end--;
            return new Decoded(new WireCore.Body("json", raw, List.of(), null), raw instanceof Map ? List.of(new Frame(WireCore.map(raw), end)) : List.of());
        }
        if (text.indexOf('\n') >= 0) {
            Decoded lines = lines(bytes, false); if (!lines.frames.isEmpty()) return lines;
        }
        return new Decoded(new WireCore.Body("text", null, List.of(), text), List.of());
    }
    private static boolean whitespace(byte b) { return b == ' ' || b == '\t' || b == '\n' || b == '\r'; }
    private static Decoded lines(byte[] bytes, boolean sse) {
        List<Frame> frames = new ArrayList<>();
        int start = 0; StringBuilder data = new StringBuilder(); String event = null;
        for (int i = 0; i < bytes.length; i++) {
            if (bytes[i] != '\n' && (!sse || bytes[i] != '\r')) continue;
            int end = i + 1;
            if (sse && bytes[i] == '\r' && end < bytes.length && bytes[end] == '\n') { end++; i++; }
            int length = end - start;
            while (length > 0 && (bytes[start + length - 1] == '\n' || bytes[start + length - 1] == '\r')) length--;
            String line = new String(bytes, start, length, StandardCharsets.UTF_8); start = end;
            if (!sse) { add(frames, line, null, end); continue; }
            if (line.isEmpty()) {
                add(frames, data.toString(), event, end); data.setLength(0); event = null;
            } else if (line.startsWith("data:")) {
                if (!data.isEmpty()) data.append('\n');
                data.append(line.substring(line.startsWith("data: ") ? 6 : 5));
            } else if (line.startsWith("event:")) event = line.substring(line.startsWith("event: ") ? 7 : 6);
        }
        // An unfinished SSE block or NDJSON line is not observed, even if its JSON is complete.
        return new Decoded(new WireCore.Body(sse ? "sse" : "ndjson", null, frames.stream().map(Frame::value).toList(), null), frames);
    }
    private static void add(List<Frame> frames, String text, String event, int end) {
        Object raw = WireCore.parse(text);
        if (!(raw instanceof Map)) return;
        Map<String, Object> value = new LinkedHashMap<>(WireCore.map(raw));
        if (event != null && !event.isEmpty() && !value.containsKey("type") && !value.containsKey("event_type")) value.put("type", event);
        frames.add(new Frame(value, end));
    }
    private static List<Frame> array(byte[] bytes, int offset) {
        List<Frame> frames = new ArrayList<>(); int depth = 0, start = -1; boolean quoted = false, escaped = false;
        for (int i = offset; i < bytes.length; i++) {
            byte b = bytes[i];
            if (start < 0) {
                if (whitespace(b) || b == ',') continue;
                if (b != '{') break;
                start = i; depth = 1; continue;
            }
            if (quoted) {
                if (escaped) escaped = false;
                else if (b == '\\') escaped = true;
                else if (b == '"') quoted = false;
            } else if (b == '"') quoted = true;
            else if (b == '{' || b == '[') depth++;
            else if ((b == '}' || b == ']') && --depth == 0) {
                add(frames, new String(bytes, start, i + 1 - start, StandardCharsets.UTF_8), null, i + 1); start = -1;
            }
        }
        return frames;
    }
    private StreamFrames() { }
}
