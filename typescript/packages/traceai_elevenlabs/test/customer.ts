import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { context } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { register, ProjectType } from "@traceai/fi-core";
import { ElevenLabsClient } from "@elevenlabs/elevenlabs-js";
import { ElevenLabsInstrumentation } from "@traceai/elevenlabs";

const contextManager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(contextManager);
const provider = register({
  projectName: "speech-app",
  projectType: ProjectType.OBSERVE,
  endpoint:
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT ??
    "https://api.futureagi.com/tracer/v1/traces",
});
const instrumentation = new ElevenLabsInstrumentation({
  tracerProvider: provider,
});
const client = instrumentation.wrapClient(
  new ElevenLabsClient({
    apiKey: process.env.ELEVENLABS_API_KEY,
    baseUrl: process.env.ELEVENLABS_BASE_URL,
  }),
);

async function main() {
  try {
    await provider
      .getTracer("speech-app")
      .startActiveSpan("Create greeting", async (parent) => {
        try {
          const audio = await client.textToSpeech.convert(
            process.env.ELEVENLABS_VOICE_ID!,
            {
              text: "Welcome! Your appointment is confirmed.",
              modelId: "eleven_multilingual_v2",
              outputFormat: "mp3_44100_128",
            },
          );
          await pipeline(audio, createWriteStream("speech.mp3"));
        } finally {
          parent.end();
        }
      });
  } finally {
    instrumentation.shutdown();
    await provider.shutdown();
    context.disable();
    contextManager.disable();
  }
}
void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
