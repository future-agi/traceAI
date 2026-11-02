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
