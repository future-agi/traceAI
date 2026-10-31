package ai.traceai.auto;

import java.io.*;
import java.util.*;
import java.util.zip.*;

/** Decode only copied bytes, never the application's stream; bound decompressed output too. */
final class EncodedBody {
    record Decoded(byte[] bytes, boolean truncated, String undecoded) {}
    static Decoded decode(byte[] bytes, Map<String, String> headers, int limit, boolean partial) {
        String encoding = WireCore.rawHeader(headers, "content-encoding").trim().toLowerCase(Locale.ROOT);
        if (bytes == null || encoding.isEmpty() || encoding.equals("identity")) return new Decoded(bytes, false, null);
        try {
            String[] encodings = encoding.split("[,\\r\\n]+");
            for (int i = encodings.length - 1; i >= 0; i--) {
                String name = encodings[i].trim();
                Decoded decoded;
                try { decoded = read(bytes, name, limit, partial, false); }
                catch (IOException error) {
                    if (!name.equals("deflate")) throw error;
                    decoded = read(bytes, name, limit, partial, true);
                }
                if (decoded.truncated) return decoded;
                bytes = decoded.bytes;
            }
            return new Decoded(bytes, false, null);
        } catch (Throwable ignored) { return new Decoded(null, false, encoding); }
    }
    private static Decoded read(byte[] bytes, String name, int limit, boolean partial, boolean raw) throws Exception {
        InputStream source = new ByteArrayInputStream(bytes);
        Inflater inflater = name.equals("deflate") ? new Inflater(raw) : null;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (InputStream decoded = switch (name) {
            case "identity" -> source;
            case "gzip", "x-gzip" -> new GZIPInputStream(source);
            case "deflate" -> new InflaterInputStream(source, inflater);
            case "br" -> optional("org.brotli.dec.BrotliInputStream", source);
            case "zstd" -> optional("com.github.luben.zstd.ZstdInputStream", source);
            default -> throw new IOException("Unsupported content encoding");
        }) {
            byte[] buffer = new byte[8192]; int count;
            try {
                while ((count = decoded.read(buffer)) != -1) {
                    if (count == 0) continue;
                    if (count > limit - out.size()) return new Decoded(null, true, null);
                    out.write(buffer, 0, count);
                }
            } catch (IOException error) { if (!partial || out.size() == 0) throw error; }
            return new Decoded(out.toByteArray(), false, null);
        } finally { if (inflater != null) inflater.end(); }
    }
    private static InputStream optional(String name, InputStream source) throws Exception {
        return (InputStream)Class.forName(name, false, Thread.currentThread().getContextClassLoader()).getConstructor(InputStream.class).newInstance(source);
    }
    private EncodedBody() {}
}
