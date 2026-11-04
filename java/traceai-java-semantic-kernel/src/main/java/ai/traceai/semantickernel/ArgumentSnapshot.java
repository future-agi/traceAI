package ai.traceai.semantickernel;

import java.lang.reflect.Array;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Collection;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/** A bounded JSON-shaped copy, never a user object passed to Gson or toString(). */
final class ArgumentSnapshot {
    private static final int MAX_DEPTH = 32;
    private static final int MAX_NODES = 1024;

    private final Map<Object, Boolean> ancestors = new IdentityHashMap<>();
    private int remaining = MAX_NODES;

    private ArgumentSnapshot() { }

    static Object capture(Object value) {
        try {
            return new ArgumentSnapshot().copy(value, 0);
        } catch (RuntimeException ignored) {
            // A failing container accessor must not prevent capture of other arguments.
            return "[unavailable]";
        }
    }

    private Object copy(Object value, int depth) {
        if (remaining-- <= 0) {
            return "[truncated]";
        }
        if (value == null || value instanceof String || value instanceof Boolean) {
            return value;
        }
        if (value instanceof Character || value instanceof Enum<?>) {
            return value instanceof Enum<?> ? ((Enum<?>) value).name() : value.toString();
        }
        // Only known scalar implementations: a custom Number can have a recursive formatter.
        Class<?> type = value.getClass();
        if (type == Byte.class || type == Short.class || type == Integer.class || type == Long.class
                || type == BigInteger.class || type == BigDecimal.class) {
            return value;
        }
        if (type == Float.class && Float.isFinite((Float) value)
                || type == Double.class && Double.isFinite((Double) value)) {
            return value;
        }
        if (!(value instanceof Map<?, ?> || value instanceof Collection<?> || type.isArray())) {
            // Arbitrary beans/formatters are not safe to inspect reflectively or stringify.
            return "[unsupported]";
        }
        if (depth >= MAX_DEPTH) {
            return "[max depth]";
        }
        if (ancestors.put(value, Boolean.TRUE) != null) {
            return "[circular]";
        }
        try {
            if (value instanceof Map<?, ?>) {
                Map<String, Object> result = new LinkedHashMap<>();
                for (Map.Entry<?, ?> entry : ((Map<?, ?>) value).entrySet()) {
                    if (remaining <= 0) {
                        break;
                    }
                    // Never invoke a user-defined key formatter. JSON object keys are strings.
                    if (entry.getKey() instanceof String) {
                        result.put((String) entry.getKey(), copy(entry.getValue(), depth + 1));
                    } else {
                        remaining--;
                    }
                }
                return result;
            }
            var result = new ArrayList<>();
            if (value instanceof Collection<?>) {
                for (Object item : (Collection<?>) value) {
                    if (remaining <= 0) {
                        result.add("[truncated]");
                        break;
                    }
                    result.add(copy(item, depth + 1));
                }
            } else {
                for (int index = 0; index < Array.getLength(value); index++) {
                    if (remaining <= 0) {
                        result.add("[truncated]");
                        break;
                    }
                    result.add(copy(Array.get(value, index), depth + 1));
                }
            }
            return result;
        } finally {
            // Track only the current path so shared, non-cyclic values retain their content.
            ancestors.remove(value);
        }
    }
}
