package dev.langchain4j.agent.tool;

/** Test stand-in with the LangChain4j class's name and accessors; the agent matches the library by name. */
public final class ToolExecutionRequest {
    private final String id, name, arguments;
    public ToolExecutionRequest(String id, String name, String arguments) { this.id = id; this.name = name; this.arguments = arguments; }
    public String id() { return id; }
    public String name() { return name; }
    public String arguments() { return arguments; }
}
