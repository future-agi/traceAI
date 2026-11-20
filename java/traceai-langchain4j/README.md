# traceai-langchain4j

Future AGI tracing for [LangChain4j](https://docs.langchain4j.dev): `TracedChatLanguageModel`, `TracedEmbeddingModel`, `TracedAiServices` and `TracedToolExecutor`. LangChain4j is a `provided` dependency; the application brings its own (built and tested with 0.35.0).

## Tool execution spans

`TracedToolExecutor` wraps a `ToolExecutor` and records one `execute_tool <tool name>` span per execution: an INTERNAL span, child of the current span and current while the tool runs, with `fi.span.kind=TOOL`, `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id` (the model's call id), `gen_ai.tool.description` (when the `ToolSpecification` is known), the arguments as `input.value` and the result as `output.value`. A failure is recorded with status `ERROR` and the original exception is rethrown.

AiServices builds its own executors for `tools(Object...)`, so pass it the traced map instead:

```java
Assistant assistant = AiServices.builder(Assistant.class)
    .chatLanguageModel(model)
    .tools(TracedToolExecutor.forTools(TraceAI.getTracer(), new WeatherTools()))
    .build();
```

`forTools` covers the `@Tool` methods of the objects you pass. For tools you already hold as a `Map<ToolSpecification, ToolExecutor>`, use `TracedToolExecutor.wrapAll(tools, tracer)`; for a single executor, `new TracedToolExecutor(executor, specification, tracer)`.

Content goes through `FITracer`: `hideInputs` / `hideInputMessages` withhold the arguments and the description, `hideOutputs` / `hideOutputMessages` withhold the result, and `hideErrorDetails` keeps exception text out. Wrap a tool once. With the zero-code `traceai-javaagent` also attached, the agent skips a call that already runs under this wrapper's span, so there is one span per execution.
