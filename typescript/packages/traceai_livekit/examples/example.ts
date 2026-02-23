/**
 * Example of using LiveKit instrumentation with FI tracing.
 *
 * This example shows how to instrument the LiveKit SDK
 * for observability with OpenTelemetry.
 */

import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { LiveKitInstrumentation } from "@traceai/fi-instrumentation-livekit";

// Set up OpenTelemetry tracing
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(new ConsoleSpanExporter()));
provider.register();

// Create and register the instrumentation
const instrumentation = new LiveKitInstrumentation({
  traceConfig: {
    hideInputs: false,
    hideOutputs: false,
  },
});
instrumentation.setTracerProvider(provider);
instrumentation.enable();

async function main() {
  // Import LiveKit after instrumentation is set up
  // const { Room, RoomEvent } = await import("livekit-client");
  // OR for server-side:
  // const { Room, AudioSource, VideoSource } = await import("@livekit/rtc-node");

  console.log("LiveKit instrumentation example");
  console.log("================================");
  console.log("");
  console.log("This example demonstrates how to set up LiveKit instrumentation.");
  console.log("To run this with actual LiveKit calls, you would:");
  console.log("");
  console.log("1. Install the livekit-client package (for browser/client):");
  console.log("   npm install livekit-client");
  console.log("");
  console.log("   OR for server-side Node.js agents:");
  console.log("   npm install @livekit/rtc-node");
  console.log("");
  console.log("2. Set up your environment:");
  console.log("   export LIVEKIT_URL=wss://your-livekit-server.com");
  console.log("   export LIVEKIT_API_KEY=your-api-key");
  console.log("   export LIVEKIT_API_SECRET=your-api-secret");
  console.log("");
  console.log("3. Uncomment the code below and run:");
  console.log("");

  /*
  // Client-side example (livekit-client)
  const room = new Room();

  // Set up event handlers
  room.on(RoomEvent.Connected, () => {
    console.log("Connected to room");
  });

  room.on(RoomEvent.ParticipantConnected, (participant) => {
    console.log("Participant joined:", participant.identity);
  });

  // Connect to room - will be traced
  const token = "your-access-token";
  await room.connect(process.env.LIVEKIT_URL!, token);
  console.log("Connected to room:", room.name);

  // Publish audio track - will be traced
  const audioTrack = await createLocalAudioTrack();
  await room.localParticipant.publishTrack(audioTrack, {
    name: "microphone",
  });
  console.log("Published audio track");

  // Disconnect - will be traced
  await room.disconnect();
  console.log("Disconnected from room");
  */

  /*
  // Server-side agent example (@livekit/rtc-node)
  const room = new Room();
  await room.connect(url, token);

  // Create audio source for agent output
  const audioSource = new AudioSource(24000, 1);
  const audioTrack = LocalAudioTrack.createAudioTrack("agent-audio", audioSource);
  await room.localParticipant.publishTrack(audioTrack);

  // Capture audio frames - will be traced
  const frame = new AudioFrame(samples, 24000, 1, 480);
  await audioSource.captureFrame(frame);
  */

  console.log("Instrumentation is active and ready to trace LiveKit calls.");
}

main().catch(console.error);
