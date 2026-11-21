// Loaded with -r, after the agent. It loads the OpenAI Agents SDK, which installs its own global undici dispatcher, and turns on the SDK's own
// tracing (the SDK turns it off when NODE_ENV is test). The trace processor the agent adds when the SDK loads is left in place; the SDK's default
// exporter for OpenAI's servers sends nothing, because no tracing API key is set (the test runs with OPENAI_API_KEY unset). It registers no OpenTelemetry.
const agents = require('@openai/agents');
agents.setTracingDisabled(false);
