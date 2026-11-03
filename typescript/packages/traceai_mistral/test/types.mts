import { register, ProjectType } from "@traceai/fi-core";
import { MistralInstrumentation } from "@traceai/mistral";
import * as sdk from "mistral-current";

// Compile-only contract: explicit wrapping must preserve the native client type.
const provider = register({ projectName: "mistral-app", projectType: ProjectType.OBSERVE });
const instrumentation = new MistralInstrumentation();
instrumentation.setTracerProvider(provider);
const client: sdk.Mistral = instrumentation.instrumentClient(new sdk.Mistral({ apiKey: "synthetic" }));
instrumentation.manuallyInstrument(sdk);
const response = await client.chat.complete({ model: "mistral-small-latest", messages: [{ role: "user", content: "Hello" }] }, { signal: new AbortController().signal });
console.log(response.choices[0]?.message.content);
const stream = await client.chat.stream({ model: "mistral-small-latest", messages: [{ role: "user", content: "Hello" }] });
for await (const event of stream) {
  const content = event.data.choices[0]?.delta.content;
  if (typeof content === "string") process.stdout.write(content);
}
instrumentation.disable();
await provider.shutdown();
