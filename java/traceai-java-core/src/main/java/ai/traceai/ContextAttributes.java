package ai.traceai;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Thread-local context attributes that are automatically applied to all spans
 * created within a scope. Provides static methods that return {@link AutoCloseable}
 * scopes for use in try-with-resources blocks.
 *
 * <p>Usage:</p>
 * <pre>
 * try (var ignored = ContextAttributes.usingSession("my-session-id")) {
 *     // All spans created here will have session.id = "my-session-id"
 *     tracer.startSpan("my-span", FISpanKind.LLM);
 * }
 * </pre>
 */
public final class ContextAttributes {

    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();

    private static final ThreadLocal<String> sessionId = new ThreadLocal<>();
    private static final ThreadLocal<String> userId = new ThreadLocal<>();
    private static final ThreadLocal<String> metadata = new ThreadLocal<>();
    private static final ThreadLocal<List<String>> tags = new ThreadLocal<>();

    private ContextAttributes() {
        throw new UnsupportedOperationException("Utility class");
    }

    /**
     * Sets the session ID for all spans created within the returned scope.
     *
     * @param id the session ID
     * @return an AutoCloseable scope that restores the previous value on close
     */
    public static AutoCloseable usingSession(String id) {
        return new Scope<>(sessionId, id);
    }

    /**
     * Sets the user ID for all spans created within the returned scope.
     *
     * @param id the user ID
     * @return an AutoCloseable scope that restores the previous value on close
     */
    public static AutoCloseable usingUser(String id) {
        return new Scope<>(userId, id);
    }

    /**
     * Sets metadata for all spans created within the returned scope.
     *
     * @param meta the metadata map
     * @return an AutoCloseable scope that restores the previous value on close
     */
    public static AutoCloseable usingMetadata(Map<String, Object> meta) {
        String json = (meta != null) ? GSON.toJson(meta) : null;
        return new Scope<>(metadata, json);
    }

    /**
     * Sets tags for all spans created within the returned scope.
     *
     * @param tagList the list of tags
     * @return an AutoCloseable scope that restores the previous value on close
     */
    public static AutoCloseable usingTags(List<String> tagList) {
        return new Scope<>(tags, tagList);
    }

    /**
     * Returns the current context attributes as a map of span attribute keys to values.
     * Only includes attributes that have been set (non-null).
     *
     * @return a map of attribute key to value
     */
    public static Map<String, String> getAttributesFromContext() {
        Map<String, String> attrs = new LinkedHashMap<>();
        String sid = sessionId.get();
        if (sid != null && !sid.isEmpty()) {
            attrs.put(SemanticConventions.SESSION_ID, sid);
            attrs.put(SemanticConventions.GEN_AI_CONVERSATION_ID, sid);
        }
        String uid = userId.get();
        if (uid != null && !uid.isEmpty()) {
            attrs.put(SemanticConventions.USER_ID, uid);
        }
        String meta = metadata.get();
        if (meta != null && !meta.isEmpty()) {
            attrs.put(SemanticConventions.METADATA, meta);
        }
        List<String> t = tags.get();
        if (t != null && !t.isEmpty()) {
            attrs.put(SemanticConventions.TAG_TAGS, GSON.toJson(t));
        }
        return attrs;
    }

    /**
     * Internal scope that saves and restores a ThreadLocal value.
     */
    private static class Scope<T> implements AutoCloseable {
        private final ThreadLocal<T> local;
        private final T previous;

        Scope(ThreadLocal<T> local, T value) {
            this.local = local;
            this.previous = local.get();
            local.set(value);
        }

        @Override
        public void close() {
            if (previous == null) {
                local.remove();
            } else {
                local.set(previous);
            }
        }
    }
}
