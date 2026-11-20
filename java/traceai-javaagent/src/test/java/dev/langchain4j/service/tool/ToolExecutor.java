package dev.langchain4j.service.tool;

import dev.langchain4j.agent.tool.ToolExecutionRequest;

/** Test stand-in with the LangChain4j interface's name and signature. */
public interface ToolExecutor {
    String execute(ToolExecutionRequest toolExecutionRequest, Object memoryId);
}
