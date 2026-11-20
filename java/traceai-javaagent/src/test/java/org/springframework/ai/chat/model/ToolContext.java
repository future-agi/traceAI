package org.springframework.ai.chat.model;

import java.util.Map;

/** Test stand-in with the Spring AI class's name. */
public final class ToolContext {
    private final Map<String, Object> context;
    public ToolContext(Map<String, Object> context) { this.context = context; }
    public Map<String, Object> getContext() { return context; }
}
