import * as floor from "@elevenlabs/sdk-floor";
import * as current from "@elevenlabs/elevenlabs-js";
import { ElevenLabsInstrumentation } from "@traceai/elevenlabs";

const instrumentation = new ElevenLabsInstrumentation();
instrumentation.manuallyInstrument(floor);
instrumentation.manuallyInstrument(current);
const oldClient: floor.ElevenLabsClient = instrumentation.wrapClient(
  new floor.ElevenLabsClient({ apiKey: "synthetic" }),
);
const newClient: current.ElevenLabsClient = instrumentation.wrapClient(
  new current.ElevenLabsClient({ apiKey: "synthetic" }),
);

async function contract(
  client: floor.ElevenLabsClient | current.ElevenLabsClient,
) {
  const request = { text: "synthetic", modelId: "eleven_multilingual_v2" };
  const pending = client.textToSpeech.convert("voice-id", request);
  const raw = await pending.withRawResponse();
  const stream: ReadableStream<Uint8Array> = raw.data;
  await stream.cancel();
  await pending
    .then((value) => value.getReader())
    .catch(() => undefined)
    .finally(() => undefined);
  await client.textToSpeech.stream("voice-id", request);
  await client.textToSpeech.convertWithTimestamps("voice-id", request);
  const chunks = await client.textToSpeech.streamWithTimestamps(
    "voice-id",
    request,
  );
  for await (const chunk of chunks) {
    const audio: string = chunk.audioBase64;
    void audio;
  }
  await client.speechToText.convert({
    modelId: "scribe_v2",
    file: new Blob(["synthetic"]),
  });
}
void contract;
void oldClient;
void newClient;
