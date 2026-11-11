# @traceai/fi-instrumentation-livekit

Trace LiveKit room connections, track publishing, and audio/video frame capture in Future AGI from a Node.js application.

## Install

Use Node.js 18 or later, or the newer Node version required by your LiveKit SDK.

```sh
npm install @traceai/fi-instrumentation-livekit @traceai/fi-core @livekit/rtc-node
```

The instrumentor supports `@livekit/rtc-node >=0.3.0 <2` and `livekit-client >=1.0.0 <3`. Install only the SDK your application uses. It does not instrument `livekit-server-sdk` administration APIs.

For LiveKit Agents' LLM and session traces, use the framework's native OpenTelemetry integration. This package adds RTC operation spans; it does not create conversation roots, transcripts, or LLM spans.

## Connect a room

Set `FI_API_KEY` and `FI_SECRET_KEY` in your server environment. Set `LIVEKIT_URL` to your LiveKit server URL and `LIVEKIT_TOKEN` to a room access token. Keep these credentials out of client-side bundles.

Save the following as `trace-room.cjs` and run `node trace-room.cjs`:

```javascript
const { register, ProjectType } = require("@traceai/fi-core");
const { LiveKitInstrumentation } = require("@traceai/fi-instrumentation-livekit");

const provider = register({
  projectName: "livekit-app",
  projectType: ProjectType.OBSERVE,
  endpoint: "https://api.futureagi.com/tracer/v1/traces",
});
const instrumentation = new LiveKitInstrumentation();
instrumentation.setTracerProvider(provider);

async function main() {
  const rtc = await import("@livekit/rtc-node");
  instrumentation.manuallyInstrument(rtc);
  const room = new rtc.Room();
  try {
    await room.connect(process.env.LIVEKIT_URL, process.env.LIVEKIT_TOKEN);
    // Publish tracks and capture frames using your application's normal LiveKit code.
  } finally {
    try {
      await room.disconnect();
    } finally {
      instrumentation.disable();
      try {
        await rtc.dispose?.();
      } finally {
        await provider.shutdown();
      }
    }
  }
}
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
```

Use a current RTC SDK release for standalone scripts. Older releases without `dispose()` can keep the process running after disconnection. In a long-running application, dispose the shared RTC runtime only when all rooms and media work have finished.

Open the `livekit-app` project in Future AGI to inspect `room.connect` and `room.disconnect` spans. A connect span ends when the connect call settles, not when the call's room eventually disconnects. Failed operations have error status. Audio and video capture spans contain frame metadata, not media bytes.

If your application already has a tracer provider, pass it to `setTracerProvider()` rather than registering a second one. Reuse its async context manager to keep RTC operations under the active application span.

## Use the client SDK

For CommonJS, create the instrumentor and set its provider before requiring `livekit-client`; the automatic hook instruments newly constructed rooms and local participants.

For ESM or a previously loaded client SDK, use the module returned by `manuallyInstrument()`:

```javascript
const client = instrumentation.manuallyInstrument(await import("livekit-client"));
const room = new client.Room();
```

`livekit-client` implements room methods on each instance. If you already have a room, use `instrumentation.instrumentRoom(room)` instead. Repeated registration does not add duplicate spans. Existing instrumented rooms resume tracing when you call `enable()` after `disable()`. Manually registered SDK modules should also be registered again to restore their participant and media hooks.

## Upload an existing LiveKit Agents recording (optional)

LiveKit Agents for Node can record a session to `audio.ogg` in the job's session directory when you start the session with recording on (`session.start({ ..., record: true })`, or the project's LiveKit Cloud setting). This package can upload **that existing file** to Future AGI at session end. It never turns recording on, never starts or stops the recorder, and never deletes the file. Attachments stay off unless you enable them on your provider.

Supported `@livekit/agents` versions: 1.8.0, 1.8.1 and 1.9.0 to 1.9.5. Any other version reports `unsupported_version` and uploads nothing. Linux and macOS only.

```javascript
const agents = require("@livekit/agents");
const { bindLiveKitSessionRecording } = require("@traceai/fi-instrumentation-livekit/agents");

// Inside your agent entry, after `await session.start({ agent, room: ctx.room, record: true })`:
const recording = bindLiveKitSessionRecording(session, ctx, {
  manager: provider.attachments, // the same manager your Future AGI provider created
  agents,                        // the @livekit/agents module your application uses
  ownedRecordingFiles: true,     // explicit opt-in; anything else reports "disabled"
  finalizationTimeoutMs: 30000,  // upload deadline at session end, at most 30 s
});
// recording.status is "session_bound" when it will run at session end.
```

How it works:

- Bind right after `session.start(...)` returns. The session's `agent_session` span is captured then, and the upload is linked to it. Point LiveKit's telemetry at your Future AGI provider (`agents.telemetry.setTracerProvider(provider)`) so that span is exported; if the span is not recording, the binding reports `correlation_unavailable`.
- At session end, LiveKit closes the recorder, runs your `onSessionEnd`, then calls its own session-end step. The binding runs at the start of that step, uploads `audio.ogg` (mixed user and agent audio, up to the attachment service's single-upload limit), and then lets LiveKit continue. If your `onSessionEnd` moves or deletes the file, the binding reports `source_unavailable`.
- Only the job's primary session, its own recorder and the fixed `audio.ogg` in the job's session directory qualify. A symlink, a hard link, or a file swapped during upload fails closed.
- It uploads nothing when attachments are off, when your trace config hides inputs or outputs or redacts PII, when the server policy denies it, when LiveKit project or session redaction is on, when `LIVEKIT_TELEMETRY_ALLOW_PII` is false, or when session audio recording is off.
- Give your worker a shutdown grace longer than `finalizationTimeoutMs` plus your own session-end callbacks. A process killed before the upload finishes loses it.
- `recording.status` is content-free. `validating` means the file reached Future AGI and is being checked; it is not yet playable. `recording.close()` detaches the binding without touching the session.

## Control sensitive data

Tokens, URL credentials, URL paths, query strings, and raw audio/video are not captured as span attributes. Room names, participant identifiers, track names, publish options, and SDK error messages can still contain sensitive information. Use your collector's attribute filtering for identifying metadata, and configure content/error redaction when constructing the instrumentor:

```javascript
const instrumentation = new LiveKitInstrumentation({
  traceConfig: {
    hideInputs: true,
    hideOutputs: true,
    hideErrorDetails: true,
  },
});
```

Frame capture produces one span per frame. Apply sampling in your tracer provider for high-volume audio/video applications. Shut down the provider only when the application is finished so pending spans can be exported.

## License

Apache-2.0.
