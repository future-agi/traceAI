package ai.traceai.privacy;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Level;
import java.util.logging.Logger;

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
 * <p>Values shorter than {@link #MIN_LENGTH} are not registered (one value-free warning per process
 * says so). The registry is bounded to {@code 1024} values and 4 MiB of characters; when a bound is
 * exceeded it fails closed, the replacing exporter drops the spans it would have exported, and one
 * value-free warning per process names the bound.</p>
 *
 * <p>A capture-time length cap must use {@link #cap} (full value available) or {@link #sealCut} (text
 * already cut, for example while a stream is accumulated), so that a secret straddling the cut leaves
 * no prefix behind.</p>
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
    private static final Logger LOG = Logger.getLogger(KnownSecrets.class.getName());
    private static final AtomicBoolean WARNED_SHORT = new AtomicBoolean();
    private static final AtomicBoolean WARNED_DROP = new AtomicBoolean();

    private final Set<String> values = new LinkedHashSet<>();
    private int characters;
    private boolean unavailable;
    private String failure = "the registry was marked unavailable";
    private volatile Snapshot cached = new Snapshot(new Secret[0], false, null);

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
        if (value == null || values.contains(value)) {
            return;
        }
        if (value.length() < MIN_LENGTH) {
            if (!value.isEmpty() && WARNED_SHORT.compareAndSet(false, true)) {
                LOG.warning("TraceAI: a value registered as a secret is shorter than " + MIN_LENGTH
                        + " characters and is ignored; values shorter than that are not replaced in exported spans");
            }
            return;
        }
        if (unavailable) {
            return;
        }
        if (value.length() > MAX_CHARACTERS || values.size() + 1 > MAX_VALUES
                || characters + value.length() > MAX_CHARACTERS) {
            failure = values.size() + 1 > MAX_VALUES
                    ? "more than " + MAX_VALUES + " known secrets were registered"
                    : "the known secrets exceed " + MAX_CHARACTERS + " characters";
            unavailable = true;
            cached = null;
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

    /** Test hook: forgets which one-time warnings were already logged. */
    static void resetWarningsForTest() {
        WARNED_SHORT.set(false);
        WARNED_DROP.set(false);
    }

    /**
     * Caps a fully available value with the current process-wide secrets; see {@link Snapshot#cap}.
     * @param value the full value
     * @param limit the longest result
     * @return the replaced, cut value
     */
    public static String cap(String value, int limit) {
        return value == null || value.length() <= limit ? value : PROCESS.snapshot().cap(value, limit);
    }

    /**
     * Finishes already-cut text with the current process-wide secrets; see {@link Snapshot#sealCut}.
     * @param cutText the retained prefix
     * @return the replaced text ending with the marker
     */
    public static String sealCut(String cutText) {
        return PROCESS.snapshot().sealCut(cutText);
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
            cached = new Snapshot(secrets, unavailable, unavailable ? failure : null);
        }
        return cached;
    }

    /** Immutable, longest-first secrets taken at one instant. */
    public static final class Snapshot {
        private final Secret[] secrets;
        private final boolean unavailable;
        private final String failure;

        private Snapshot(Secret[] secrets, boolean unavailable, String failure) {
            this.secrets = secrets;
            this.unavailable = unavailable;
            this.failure = failure;
        }

        /**
         * Logs, once per process and without any value, that spans are dropped because the registry
         * failed closed. Called by exporters that drop spans.
         */
        public void warnDropped() {
            if (unavailable && WARNED_DROP.compareAndSet(false, true)) {
                LOG.log(Level.WARNING, "TraceAI: spans are dropped from export because {0}; known-secret "
                        + "replacement cannot run safely. Register fewer or shorter secrets.", failure);
            }
        }

        /** @return true when the registry failed closed and nothing may be exported */
        public boolean isUnavailable() {
            return unavailable;
        }

        /** @return true when no secret is registered */
        public boolean isEmpty() {
            return secrets.length == 0;
        }

        /** @return the length of the longest known secret, 0 when none */
        public int longest() {
            return secrets.length == 0 ? 0 : secrets[0].value.length();
        }

        private String scrubOrRaw(String text) {
            return unavailable ? text : scrub(text);
        }

        /**
         * Caps a value that is fully available: known secrets are replaced in the whole value first and
         * only then is it cut, so no secret can straddle the cut. A cut value ends with
         * {@link #TRUNCATION_MARKER}.
         * @param value the full value, may be null
         * @param limit the longest result, marker included
         * @return the value, replaced and cut to {@code limit} characters
         */
        public String cap(String value, int limit) {
            if (value == null || value.length() <= limit) {
                return value;
            }
            String clean = scrubOrRaw(value);
            if (clean.length() <= limit) {
                return clean;
            }
            return sealCut(clean.substring(0, limit - TRUNCATION_MARKER.length()));
        }

        /**
         * Finishes text that was already cut at a capture-time cap (so the rest of it is gone): drops the
         * last {@code longest() - 1} characters, which could be the start of a secret that continued past
         * the cut, replaces known secrets in what remains and appends {@link #TRUNCATION_MARKER}.
         * @param cutText the retained prefix
         * @return the replaced text ending with the marker
         */
        public String sealCut(String cutText) {
            int keep = Math.max(0, cutText.length() - Math.max(0, longest() - 1));
            if (keep > 0 && Character.isHighSurrogate(cutText.charAt(keep - 1))) {
                keep--;
            }
            // Scrubbed with the marker in place, so a secret that is cut off here also loses its trailing prefix.
            return scrubOrRaw(cutText.substring(0, keep) + TRUNCATION_MARKER);
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
