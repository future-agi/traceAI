# @traceai/bedrock

OpenTelemetry instrumentation for AWS Bedrock. Part of traceAI, Future AGI's OpenTelemetry instrumentation libraries.

[Documentation](https://docs.futureagi.com/docs/integrations/traceai/bedrock) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_bedrock) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install

```sh
npm install @traceai/bedrock @traceai/fi-core @opentelemetry/instrumentation @opentelemetry/api@^1.9.0 @opentelemetry/context-async-hooks@^2.0.1
npm install @aws-sdk/client-bedrock-runtime@"^3.0.0"
# Bedrock Agents (optional)
npm install @aws-sdk/client-bedrock-agent-runtime@"^3.0.0"
```

Install the AWS SDK in the application that makes Bedrock requests. Configure
AWS credentials and region using the AWS SDK's normal credential chain. For Future AGI export, set `FI_API_KEY` and
`FI_SECRET_KEY` in your environment; keep provider credentials outside source code.

## Quick start

Load this setup **before** loading the SDK you want to instrument. This CommonJS
example registers tracing; it does not make a provider request.

```javascript
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { register, ProjectType } = require("@traceai/fi-core");
// Standalone process only: if your OTel SDK already installs a context manager,
// reuse it instead of registering another one.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { BedrockInstrumentation } = require("@traceai/bedrock");
const tracerProvider = register({
  projectName: "bedrock-app",
  projectType: ProjectType.OBSERVE,
});
registerInstrumentations({
  tracerProvider, instrumentations: [new BedrockInstrumentation()],
});
```

`register()` installs a tracer provider, not an async context manager. The
explicit context manager above keeps nested asynchronous calls in one trace.
Run your application's SDK calls after this setup. Call
`await tracerProvider.shutdown()` when the application finishes to flush pending
spans. For ESM, follow the documentation for your OpenTelemetry loader and SDK
instead of assuming import order alone enables automatic patching.

## Bedrock Agents

`InvokeAgentCommand` and `InvokeInlineAgentCommand` sent with
`BedrockAgentRuntimeClient` are traced when `@aws-sdk/client-bedrock-agent-runtime` is
installed. Each call produces one AGENT span (`invoke_agent <agentId>/<aliasId>`) that
covers the whole streamed `completion`. It ends when your code has read the stream to the
end, the stream fails, or you stop reading. Your code receives the original events.

Set `enableTrace: true` on the command to also get child spans built from the stream's trace
events: model invocations (LLM spans with model, prompt, output and token usage), action
group calls (TOOL spans `execute_tool <name>`) and knowledge base lookups (RETRIEVER spans
with the retrieved documents). Prompts, answers, tool arguments and document text follow the
same hide settings as the other Bedrock spans. With `@traceai/auto`, this needs no code.

## Compatibility and privacy

This instruments the SDK hooks implemented by this package, not every API offered
by the provider. The install ranges above are intentionally limited to the module versions this
instrumentor patches; its peer declarations may be broader.
Traces can contain prompts, responses, and tool data; configure capture and
redaction before sending production traffic.

## License

Apache-2.0.
