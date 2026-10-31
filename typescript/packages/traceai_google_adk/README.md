# @traceai/fi-instrumentation-google-adk

Send Google ADK's native agent, model and tool spans to Future AGI without wrapping agent methods.

## Install

Use Node.js 22 or later and ADK 2.4.0 for the native tracing route below.

```sh
npm install @traceai/fi-instrumentation-google-adk @google/adk@2.4.0
```

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment. Keep your Google model credentials in environment variables as well.

## Configure tracing

Initialize tracing before running your agent. Use this setup once in a standalone Node process, instead of `register()` or ADK's telemetry-provider setup.

```javascript
const { configure } = require("@traceai/fi-instrumentation-google-adk");

const tracing = configure({ projectName: "google-adk-app" });

// Run your existing ADK application here.
// After its final run, flush pending spans:
// await tracing.shutdown();
```

This installs one OpenTelemetry tracer provider and an async context manager. It sends **traces only** to `https://api.futureagi.com/tracer/v1/traces`, with Future AGI authentication and project resource attributes. For a self-hosted collector, pass `endpoint` as its full HTTPS traces URL. Localhost HTTP collectors are also supported.

Do not also enable ADK's automatic OTLP exporters or install the legacy `GoogleADKInstrumentation` hooks. Two export paths can send duplicate spans or bypass this adapter's privacy settings. The adapter uses ADK's native spans; no OpenTelemetry module loader is needed for ESM applications.

Open **Observe** in Future AGI and select `google-adk-app`. A normal model-and-tool run contains the invocation, agent, model calls and tool calls in one trace.

## Content and session privacy

By default, the adapter exports span names, operation/model/tool metadata, timing, status and token counts. It removes native request/response payloads, session identifiers, descriptions, arbitrary events, error messages and stack traces from ADK spans. Agent/tool names and workflow paths remain visible; do not put sensitive data in them. Other instrumentation scopes are not redacted by this adapter.

Opt in separately to content and session correlation:

```javascript
const tracing = configure({
  projectName: "google-adk-app",
  captureContent: true,
  captureSessionId: true,
});
```

`captureContent` maps native model/tool request and response JSON to the input/output fields. `captureSessionId` maps native conversation identifiers to `session.id`. Error messages and stack traces remain excluded. These options govern the native route; the legacy instrumentor's `traceConfig` settings do not configure it.

ADK normally captures message content in its in-process spans. This adapter removes it before sending those spans through its exporter. Set `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` before running ADK if content must not enter in-process spans at all. In that mode, content opt-in cannot recover payloads, and ADK's output-token counter excludes thinking tokens. When native response usage metadata is available, the adapter includes thinking tokens in output counts and preserves the reported total; it does not invent missing totals.

## Existing OpenTelemetry applications

Keep your existing provider and wrap its trace exporter with `GoogleADKSpanExporter` before constructing your span processor:

```javascript
const { GoogleADKSpanExporter } = require("@traceai/fi-instrumentation-google-adk");

const adkExporter = new GoogleADKSpanExporter(yourTraceExporter, {
  captureContent: false,
  captureSessionId: false,
});
// Use adkExporter, rather than yourTraceExporter, in your existing span processor.
```

Your existing exporter remains responsible for its endpoint and authentication; the provider remains responsible for `project_name` and `project_type: "observe"` resource attributes. Wrap every export path that must apply this privacy policy. The wrapper neither creates spans nor changes trace/parent identifiers.

## Migrate from legacy hooks

`GoogleADKInstrumentation`, `manuallyInstrument()`, `enable()` and `disable()` remain available for existing applications, with a deprecation warning. Their old module filters are unchanged; they are not ADK 2.x support. Replace the legacy setup with `configure()` for ADK 2.4.0, or wrap your existing exporter. Native setup refuses already-patched processes; restart after removing the old setup. Legacy hooks enabled after `configure()` do not patch the module.

ADK 0.1.0 emits only basic invocation/agent spans and does not preserve their parent relationship. Upgrade ADK for model/tool coverage and correlated traces. This adapter does not manufacture spans missing from the framework.

## License

Apache-2.0.
