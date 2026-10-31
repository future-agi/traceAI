package ai.traceai.auto;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Bounded tee; binary is counted only. Multipart retains text and boundary bytes, never file data. */
final class BodyCapture {
    final int limit;
    final boolean binary;
    final Multipart multipart;
    final AudioCounter audio;
    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
    long size;
    boolean truncated;
    BodyCapture(String type, int limit, boolean audioResponse) {
        this.limit = limit; binary = WireCore.binary(type.split(";", 2)[0].trim().toLowerCase(Locale.ROOT));
        multipart = type.toLowerCase(Locale.ROOT).startsWith("multipart/") ? new Multipart(type, limit) : null;
        audio = audioResponse && !binary ? new AudioCounter(limit) : null;
    }
    synchronized void add(ByteBuffer source) {
        try {
            ByteBuffer data = source.duplicate(); int count = data.remaining(); size += count;
            if (binary) return;
            if (multipart != null) { while (data.hasRemaining()) multipart.add(data.get()); truncated |= multipart.truncated; return; }
            if (audio != null) { while (data.hasRemaining()) audio.add(data.get()); truncated |= audio.truncated; return; }
            if (truncated) return;
            int copy = Math.min(count, Math.max(0, limit - bytes.size()));
            byte[] chunk = new byte[copy]; data.get(chunk); bytes.write(chunk);
            if (count > copy) { truncated = true; bytes.reset(); }
        } catch (Throwable ignored) { truncated = true; bytes.reset(); }
    }
    byte[] body() {
        if (multipart != null) multipart.end();
        return binary || multipart != null ? null : audio != null ? audio.sanitized.toByteArray() : bytes.toByteArray();
    }

    synchronized void release() {
        bytes = new ByteArrayOutputStream();
        if (multipart != null) { multipart.fields.clear(); multipart.files.clear(); multipart.header = new ByteArrayOutputStream(); multipart.field = new ByteArrayOutputStream(); }
        if (audio != null) audio.sanitized = new ByteArrayOutputStream();
    }

    static final class Multipart {
        final int limit;
        final byte[] boundary;
        ByteArrayOutputStream header = new ByteArrayOutputStream(), field = new ByteArrayOutputStream();
        final Map<String, String> fields = new LinkedHashMap<>();
        final List<WireCore.FilePart> files = new ArrayList<>();
        int state, pending, textBytes, headerTail, parts;
        final StringBuilder suffix = new StringBuilder();
        static final int MAX_FIELD = 65536, MAX_METADATA = 1048576, MAX_PARTS = 128;
        long partBytes, fileBytes;
        String name, filename, type;
        boolean truncated, retainPart;
        Multipart(String contentType, int limit) {
            this.limit = limit;
            Matcher m = Pattern.compile("boundary=\"?([^\";]+)", Pattern.CASE_INSENSITIVE).matcher(contentType);
            String value = m.find() ? m.group(1) : "";
            boundary = ("\r\n--" + (value.length() <= 200 ? value : "")).getBytes(StandardCharsets.ISO_8859_1);
        }
        void add(byte value) {
            if (state == 0) { // first boundary line
                if (value == '\n') { state = 1; header.reset(); } return;
            }
            if (state == 1) {
                if (header.size() >= 16384) { truncated = true; state = 4; return; }
                header.write(value); headerTail = (headerTail << 8) | (value & 255);
                if (header.size() >= 4 && headerTail == 0x0d0a0d0a) {
                    retainPart = ++parts <= MAX_PARTS && header.size() <= MAX_METADATA - textBytes;
                    if (retainPart) textBytes += header.size(); else truncated = true;
                    String text = header.toString(StandardCharsets.ISO_8859_1);
                    String disposition = group(text, "(?im)^content-disposition:\\s*([^\\r\\n]*)");
                    name = parameter(disposition, "name"); filename = parameter(disposition, "filename"); type = group(text, "(?i)content-type:\\s*([^\\r\\n;]+)");
                    if (filename == null) filename = parameter(disposition, "filename*");
                    // Streamed uploads need not supply filename metadata, and a file may
                    // legitimately have a textual MIME type. Its payload is still count-only.
                    if (filename == null && ("file".equalsIgnoreCase(name) || type != null && WireCore.binary(type.trim().toLowerCase(Locale.ROOT)))) filename = "";
                    state = 2; partBytes = 0; pending = 0; field.reset(); header.reset();
                }
                return;
            }
            if (state == 3) {
                // Even "--" is only a closing delimiter when followed by a line ending
                // (optional transport padding) or EOF. "--X" remains file payload.
                suffix.append((char)(value & 255));
                String tail = suffix.toString();
                boolean closing = tail.startsWith("--");
                String padding = closing ? tail.substring(2) : tail;
                if (padding.matches("[ \\t]*\\r\\n")) {
                    finishPart(); state = closing ? 4 : 1; suffix.setLength(0); header.reset(); return;
                }
                if (suffix.length() <= 1024 && (tail.equals("-") || padding.matches("[ \\t]*\\r?"))) return;
                for (byte b : boundary) payload(b);
                suffix.setLength(0); state = 2;
                for (int i = 0; i < tail.length(); i++) add((byte)tail.charAt(i));
                return;
            }
            if (state != 2) return;
            while (pending > 0 && value != boundary[pending]) {
                // A failed boundary candidate is payload, not retained file bytes.
                payload(boundary[0]);
                for (int i = 1; i < pending; i++) payload(boundary[i]);
                pending = 0;
            }
            if (value == boundary[pending]) {
                pending++;
                if (pending == boundary.length) { pending = 0; suffix.setLength(0); state = 3; }
            } else payload(value);
        }
        void end() {
            if (state == 3 && suffix.toString().matches("--[ \\t]*")) { finishPart(); state = 4; suffix.setLength(0); }
        }
        void payload(byte value) {
            partBytes++;
            if (filename != null) { fileBytes++; return; }
            if (!retainPart) return;
            if (field.size() < MAX_FIELD && textBytes < MAX_METADATA) { field.write(value); textBytes++; } else truncated = true;
        }
        void finishPart() {
            if (name == null || !retainPart) return;
            if (filename != null) files.add(new WireCore.FilePart(name, filename, type, partBytes));
            else fields.put(name, field.toString(StandardCharsets.UTF_8));
        }
        static String parameter(String disposition, String key) {
            if (disposition == null) return null;
            Matcher m = Pattern.compile("(?:^|;)\\s*" + Pattern.quote(key) + "\\s*=\\s*(?:\"((?:\\\\.|[^\"\\\\])*)\"|([^;\\s]*))", Pattern.CASE_INSENSITIVE).matcher(disposition);
            return m.find() ? m.group(1) != null ? m.group(1).replaceAll("\\\\(.)", "$1") : m.group(2) : null;
        }
        static String group(String text, String regex) { Matcher m = Pattern.compile(regex).matcher(text); return m.find() ? m.group(1) : null; }
    }
    /** Recognizes JSON audio string fields, counting base64 alphabet characters without retaining audio. */
    static final class AudioCounter {
        boolean inString, escaped, value, target, colon, truncated, payloadSeen;
        String key;
        final StringBuilder token = new StringBuilder();
        ByteArrayOutputStream sanitized = new ByteArrayOutputStream();
        final int limit;
        long alphabet, total;
        AudioCounter(int limit) { this.limit = limit; }
        void add(byte b) {
            char c = (char)(b & 255);
            if (!inString || !target || !escaped && c == '"') {
                if (sanitized.size() < limit && !truncated) sanitized.write(b);
                else { truncated = true; sanitized.reset(); }
            }
            if (!inString) {
                if (c == ':') { colon = true; return; }
                if (c == '"') { inString = true; value = colon; target = value && key != null && PrivacyCatalog.INSTANCE.context(key, false, true, Map.of()).payload(); token.setLength(0); alphabet = 0; escaped = false; payloadSeen = false; }
                else if (c == ',' || c == '{' || c == '}') colon = false;
                return;
            }
            if (!escaped && c == '"') {
                inString = false;
                if (target) total += alphabet % 4 == 1 ? 0 : alphabet * 6 / 8;
                if (!value) key = token.toString(); else colon = false;
                return;
            }
            if (target) {
                // Preserve only the non-empty fact so a completed audio event can establish
                // first-output time. Never retain even a prefix of the raw audio.
                if (!payloadSeen) { payloadSeen = true; if (!truncated && sanitized.size() < limit) sanitized.write('!'); }
                if (c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '+' || c == '/') alphabet++;
            } else if (token.length() < 256) token.append(c);
            if (c == '\\' && !escaped) escaped = true; else escaped = false;
        }
    }
}
