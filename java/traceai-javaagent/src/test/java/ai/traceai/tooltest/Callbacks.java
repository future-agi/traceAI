package ai.traceai.tooltest;

import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;

/**
 * Application ToolCallback implementations for the agent's child-JVM tool tests. They live outside
 * ai.traceai.auto, which the agent never instruments, so the agent sees them as application classes.
 */
public final class Callbacks {
    /** A decorator that delegates every call to the callback it wraps, as tool-filtering wrappers do. */
    public static final class Wrapping implements ToolCallback {
        final ToolCallback delegate;
        public Wrapping(ToolCallback delegate) { this.delegate = delegate; }
        @Override public ToolDefinition getToolDefinition() { return delegate.getToolDefinition(); }
        @Override public String call(String input) { return delegate.call(input); }
    }
    /** A callback with an unrelated two-argument call overload; only call(String, ToolContext) is a tool call. */
    public static final class Overloaded implements ToolCallback {
        final ToolCallback delegate;
        public Overloaded(ToolCallback delegate) { this.delegate = delegate; }
        @Override public ToolDefinition getToolDefinition() { return delegate.getToolDefinition(); }
        @Override public String call(String input) { return delegate.call(input); }
        public String call(String input, String note) { return "overload:" + note; }
    }
    private Callbacks() {}
}
