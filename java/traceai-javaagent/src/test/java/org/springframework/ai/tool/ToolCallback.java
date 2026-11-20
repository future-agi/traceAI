package org.springframework.ai.tool;

import org.springframework.ai.chat.model.ToolContext;
import org.springframework.ai.tool.definition.ToolDefinition;

/** Test stand-in with the Spring AI interface's name and signatures. */
public interface ToolCallback {
    ToolDefinition getToolDefinition();
    String call(String toolInput);
    default String call(String toolInput, ToolContext toolContext) { return call(toolInput); }
}
