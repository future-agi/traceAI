package ai.traceai.privacy;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Registry of credential values that must never leave the process inside span content, with the
 * replacement applied to an immutable snapshot at export time.
 *
 * <p>Spans keep their raw values. {@link SecretReplacingSpanExporter} calls {@link Snapshot#scrub}
 * on what it exports, so a secret learned after a span was captured is still replaced, and secrets
 * that overlap each other cannot leave a partial leak: every occurrence of every secret is located
 * in the full raw value, overlapping or touching occurrences are merged into one region, and each
 * region becomes a single {@link #PLACEHOLDER}. A value ending with the SDK's truncation marker also
 * loses any trailing run that is a prefix of a secret, so a secret straddling a length cap is not
 * left half visible.</p>
 *
 * <p>Values shorter than {@link #MIN_LENGTH} are not registered. The registry is bounded; when a
 * bound is exceeded it fails closed and the replacing exporter drops what it would have exported.</p>
 */
public final class KnownSecrets {
    /** Replaces every secret occurrence. */
    public static final String PLACEHOLDER = "<redacted>";
    /** Marker the SDK appends to a value it truncated. */
    public static final String TRUNCATION_MARKER = "...[truncated]";
    /** Shortest value that is registered. */
    public static final int MIN_LENGTH = 8;

    private static final int MAX_VALUES = 1024;
    private static final int MAX_CHARACTERS = 4 * 1024 * 1024;
    private static final long MAX_WORK = 16L * 1024 * 1024;
    private static final KnownSecrets PROCESS = new KnownSecrets();

    private final Set<String> values = new LinkedHashSet<>();
    private int characters;
    private boolean unavailable;
    private volatile Snapshot cached = new Snapshot(new Secret[0], false);

    /** Creates an empty registry. Most callers want {@link #process()}. */
    public KnownSecrets() {
    }

    /** @return the process-wide registry used by {@code TraceAI} */
    public static KnownSecrets process() {
        return PROCESS;
    }

    /**
     * Registers a credential value. Null and short values are ignored.
     * @param value the credential value
     */
    public synchronized void register(String value) {
        if (value == null || value.length() < MIN_LENGTH || values.contains(value)) {
            return;
        }
        if (unavailable) {
            return;
        }
        if (value.length() > MAX_CHARACTERS || values.size() + 1 > MAX_VALUES
                || characters + value.length() > MAX_CHARACTERS) {
            failClosed();
            return;
        }
        values.add(value);
        characters += value.length();
        cached = null;
    }

    /** Marks the registry unusable: exporters drop spans instead of risking a leak. */
    public synchronized void failClosed() {
        unavailable = true;
        cached = null;
    }

    /** @return an immutable view of the secrets known now */
    public Snapshot snapshot() {
        Snapshot snapshot = cached;
        return snapshot != null ? snapshot : compile();
    }

    private synchronized Snapshot compile() {
        if (cached == null) {
            List<String> sorted = new ArrayList<>(values);
            sorted.sort((a, b) -> Integer.compare(b.length(), a.length()));
            Secret[] secrets = new Secret[sorted.size()];
            for (int i = 0; i < secrets.length; i++) {
                secrets[i] = new Secret(sorted.get(i));
            }
            cached = new Snapshot(secrets, unavailable);
        }
        return cached;
    }

    /** Immutable, longest-first secrets taken at one instant. */
    public static final class Snapshot {
        private final Secret[] secrets;
        private final boolean unavailable;

        private Snapshot(Secret[] secrets, boolean unavailable) {
            this.secrets = secrets;
            this.unavailable = unavailable;
        }

        /** @return true when the registry failed closed and nothing may be exported */
        public boolean isUnavailable() {
            return unavailable;
        }

        /** @return true when no secret is registered */
        public boolean isEmpty() {
            return secrets.length == 0;
        }

        /**
         * Replaces every occurrence of every known secret in {@code text}.
         * @param text raw text, may be null
         * @return the text with each secret region replaced by {@link #PLACEHOLDER}
         * @throws IllegalStateException if the registry failed closed
         */
        public String scrub(String text) {
            if (unavailable) {
                throw new IllegalStateException("Privacy registry unavailable");
            }
            if (text == null || text.isEmpty() || secrets.length == 0) {
                return text;
            }
            if ((long) text.length() * (secrets.length + 1) > MAX_WORK) {
                return PLACEHOLDER;
            }
            boolean truncated = text.endsWith(TRUNCATION_MARKER);
            int bodyEnd = truncated ? text.length() - TRUNCATION_MARKER.length() : text.length();
            List<int[]> regions = null;
            for (Secret secret : secrets) {
                regions = secret.find(text, bodyEnd, truncated, regions);
            }
            if (regions == null) {
                return text;
            }
            regions.sort((a, b) -> Integer.compare(a[0], b[0]));
            StringBuilder out = new StringBuilder(text.length());
            int copied = 0;
            int start = -1;
            int end = -1;
            for (int[] region : regions) {
                if (start >= 0 && region[0] <= end) {
                    end = Math.max(end, region[1]);
                    continue;
                }
                if (start >= 0) {
                    out.append(text, copied, start).append(PLACEHOLDER);
                    copied = end;
                }
                start = region[0];
                end = region[1];
            }
            out.append(text, copied, start).append(PLACEHOLDER);
            out.append(text, end, text.length());
            return out.toString();
        }
    }

    /** KMP keeps matching linear for long, repeated-prefix credentials. */
    private static final class Secret {
        private final String value;
        private final int[] prefix;

        Secret(String value) {
            this.value = value;
            prefix = new int[value.length()];
            for (int i = 1, j = 0; i < value.length(); i++) {
                while (j > 0 && value.charAt(i) != value.charAt(j)) {
                    j = prefix[j - 1];
                }
                if (value.charAt(i) == value.charAt(j)) {
                    j++;
                }
                prefix[i] = j;
            }
        }

        /** Adds [start,end) regions of full matches, plus a trailing partial match when truncated. */
        List<int[]> find(String text, int end, boolean truncated, List<int[]> regions) {
            int j = 0;
            for (int i = 0; i < end; i++) {
                while (j > 0 && text.charAt(i) != value.charAt(j)) {
                    j = prefix[j - 1];
                }
                if (text.charAt(i) == value.charAt(j)) {
                    j++;
                }
                if (j == value.length()) {
                    if (regions == null) {
                        regions = new ArrayList<>();
                    }
                    regions.add(new int[] {i + 1 - j, i + 1});
                    j = prefix[j - 1];
                }
            }
            if (truncated && j > 0) {
                if (regions == null) {
                    regions = new ArrayList<>();
                }
                regions.add(new int[] {end - j, end});
            }
            return regions;
        }
    }
}
