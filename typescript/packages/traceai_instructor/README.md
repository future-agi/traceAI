# @traceai/instructor

Trace Instructor's TypeScript structured-output requests in Future AGI. Each extraction appears as a `CHAIN` span covering parsing and validation, with provider requests and retries beneath it when provider instrumentation is enabled.

## Install

Use Node.js 20 or later and Instructor 1.x with Zod 3.

```sh
npm install @traceai/instructor @traceai/openai @traceai/fi-core @instructor-ai/instructor@^1.0.0 openai@^4.58.0 zod@^3.25.76 @opentelemetry/api@^1.9.0 @opentelemetry/instrumentation@^0.202.0 @opentelemetry/context-async-hooks@^2.0.1
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `OPENAI_API_KEY` in your environment. Keep credentials outside source code.

## Trace an extraction

Save this as `app.cjs`. Register instrumentation before loading Instructor or OpenAI.

```javascript
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { register, ProjectType } = require("@traceai/fi-core");
const { InstructorInstrumentation } = require("@traceai/instructor");
const { OpenAIInstrumentation } = require("@traceai/openai");

// In an existing OTel application, reuse its context manager and provider.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const provider = register({
  projectName: "instructor-typescript",
  projectType: ProjectType.OBSERVE,
});
const traceConfig = { hideInputs: true, hideOutputs: true };
registerInstrumentations({
  tracerProvider: provider,
  instrumentations: [
    new InstructorInstrumentation({ traceConfig }),
    new OpenAIInstrumentation({ instrumentationConfig: { enabled: false }, traceConfig }),
  ],
});

const createInstructor = require("@instructor-ai/instructor").default;
const OpenAI = require("openai").default;
const { z } = require("zod");
const client = createInstructor({ client: new OpenAI(), mode: "TOOLS" });
const request = {
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "Extract the name and age: Ada is 36." }],
  response_model: {
    name: "Person",
    schema: z.object({ name: z.string(), age: z.number() }),
  },
};

async function main() {
  try {
    const person = await client.chat.completions.create(request);
    console.log(person.name, person.age);
  } finally {
    await provider.shutdown();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
```

Run `node app.cjs`, then open the `instructor-typescript` project in Future AGI. Look for `Instructor Extract: Person` with an OpenAI request beneath it. Keep provider instrumentation enabled to see the individual model calls made during validation retries.

The example hides prompts, parsed results, and exception details on both instrumentors. To capture content, change the corresponding `traceConfig` options after reviewing your data policy. Do not put sensitive data in schema names: names identify the extraction span.

## Stream structured output

Replace the request inside `main()` with this block. Consume the stream or close it with `return()` so its extraction span finishes. A `break` in `for await` closes it automatically.

```javascript
const stream = await client.chat.completions.create({ ...request, stream: true });
for await (const partial of stream) {
  console.log(partial);
}
```

The span stays open through iteration, records the last parsed partial result, and reports iteration failures. Early closure sets `instructor.stream.cancelled = true` without claiming success. Instructor 1.0 opens the provider stream eagerly; newer versions can defer it until iteration. Instrumentation preserves that SDK behavior. Instructor does not support validation retries for streams.

## Span data and lifecycle

- Instructor is an extraction framework, not a model provider. The parent is a `CHAIN`, not a second `LLM` span. It does not claim a provider or duplicate provider token/cost attributes.
- Input capture contains the request messages, not the Zod schema, SDK client, or request-option headers. Output capture contains the parsed document without `_meta`.
- When Instructor supplies `_meta.usage`, numeric counts appear as `instructor.usage.prompt_tokens`, `instructor.usage.completion_tokens`, and `instructor.usage.total_tokens`. They describe supplied result metadata, not an aggregate of retries. Older SDK versions and some streaming paths do not supply usage; the instrumentor does not estimate it.
- Calls without `response_model` pass through without an Instructor span. The provider instrumentor can still trace them.
- `instrumentation.disable()` restores owned module factory patches. Existing clients stop starting extraction spans; in-flight operations still finish. After re-enabling instrumentation, create new Instructor clients.

For manual setup, call `instrumentation.manuallyInstrument(instructorModule)` before constructing clients. The module object must be mutable. With native ESM, use your OpenTelemetry loader, or pass a mutable `{ default: createInstructor }` object and construct clients through that object's patched `default` property. Static import order alone does not enable automatic instrumentation.

[Python guide](https://docs.futureagi.com/docs/integrations/traceai/instructor) · [Source](https://github.com/future-agi/traceAI/tree/dev/typescript/packages/traceai_instructor) · [Issues](https://github.com/future-agi/traceAI/issues)

## License

Apache-2.0.
