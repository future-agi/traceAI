// One traced Cursor SDK session (local runtime): two runs on one agent, the
// first streamed (tool spans), the second wait()-only.
//
//   export CURSOR_API_KEY=...            # Cursor dashboard: https://grok.com/dashboard
//   export FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=https://api.futureagi.com
//   node examples/basic.mjs [project dir]
import { resolve } from "node:path";
import * as CursorSDK from "@cursor/sdk";
import { ProjectType, register } from "@traceai/fi-core";
import { CursorSDKInstrumentation, shutdown } from "@traceai/cursor-sdk";

// Absolute: with a relative local.cwd the agent's shell tool reports that its
// working directory no longer exists (@cursor/sdk 1.0.38).
const cwd = resolve(process.argv[2] ?? process.cwd());
// Not global: @cursor/sdk starts its own internal spans on the global provider.
const tracerProvider = register({
  projectName: process.env.FI_PROJECT_NAME ?? "cursor-sdk-example",
  projectType: ProjectType.OBSERVE,
  setGlobalTracerProvider: false,
});
new CursorSDKInstrumentation({ tracerProvider }).manuallyInstrument(CursorSDK);

let agent;
try {
  agent = await CursorSDK.Agent.create({
    apiKey: process.env.CURSOR_API_KEY,
    // A model and its params from Cursor.models.list(). Plans with fixed model
    // settings (Plus: fast=false) fail the run with "Model unavailable" otherwise.
    model: { id: process.env.CURSOR_MODEL ?? "composer-2.5", params: [{ id: "fast", value: "false" }] },
    local: { cwd },
  });
  const run = await agent.send("List the files in this folder and read README.md, then tell me its first line.");
  for await (const event of run.stream()) {
    if (event.type === "tool_call") console.log(`tool ${event.name}: ${event.status}`);
  }
  const first = await run.wait();
  console.log("run 1:", first.status, first.usage);
  if (first.status !== "finished") throw new Error(`Cursor run 1 ${first.status}; inspect RunResult.error and the account allowance.`);

  const second = await (await agent.send("Now say which file is the largest.")).wait();
  console.log("run 2:", second.status, second.usage, "\nagent (session id):", agent.agentId);
  if (second.status !== "finished") throw new Error(`Cursor run 2 ${second.status}; inspect RunResult.error and the account allowance.`);
} finally {
  await agent?.[Symbol.asyncDispose]();
  // Export the spans, including a failed Agent.create (e.g. a bad CURSOR_API_KEY).
  await shutdown(tracerProvider);
}
