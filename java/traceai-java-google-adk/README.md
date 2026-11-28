# traceai-java-google-adk

Tracing for Google ADK for Java (1.0.0 to 1.15.0) in a manual setup.

* `TracedLlm` wraps an ADK `BaseLlm` and records one LLM span per model call: the model, the input and output messages (tool calls and tool results included), the tool definitions and the token counts.
* `TracedTool` wraps an ADK `BaseTool` and records one TOOL span per call: the tool's name and description, its arguments and its result. A wrapped built-in tool still shapes the model request.

```java
LlmAgent agent = LlmAgent.builder()
    .name("helper")
    .model(new TracedLlm(new Gemini(...)))
    .tools(new TracedTool(FunctionTool.create(Weather.class, "forecast")))
    .build();
```

## Together with the javaagent

The `traceai-javaagent` records ADK's own model and tool calls with no code. Used together, each call made through `TracedLlm` or `TracedTool` is recorded twice by default: the wrapper's span and the agent's span.

Set `FI_GOOGLE_ADK_SKIP_WRAPPED_CALLS=true` in the environment of the application (or `google_adk_skip_wrapped_calls = true` in `futureagi.toml`, or as an agent option) to have the agent record no span for a call made through the wrappers. Then the wrapper's span is the only span of the call, the agent's other spans keep their parents (spans started while a skipped tool runs, such as a sub-agent's or a lazily subscribed tool's HTTP call, are parented to the nearest span the agent records), and the agent and invocation spans keep their session, input and output.

* A model call is skipped when the model of the agent that ADK calls is a `TracedLlm`. A `TracedLlm` held inside another model class, or a model that cannot be read, is recorded by the agent.
* A tool call is skipped when it runs through a `TracedTool`: the wrapper reports each call it runs to the agent (the agent's classes are looked up on the system class path; without the agent nothing is reported and nothing is stored).
* The option is off by default and has no effect without the agent.
* `hideErrorDetails` withholds a failed tool's result from the tool's own span only: the result still reaches the next model call's input messages, which the input hide switches (`hideInputs`, `hideInputMessages`) govern.
