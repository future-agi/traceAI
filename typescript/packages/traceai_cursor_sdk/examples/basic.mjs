// One traced Cursor SDK session (local runtime): two runs on one agent, the
// first streamed (tool spans), the second wait()-only.
//
//   export CURSOR_API_KEY=...            # https://cursor.com/dashboard/api
//   export FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=https://api.futureagi.com
//   node examples/basic.mjs [project dir]
import * as CursorSDK from "@cursor/sdk";
import { ProjectType, register } from "@traceai/fi-core";
import { CursorSDKInstrumentation, shutdown } from "@traceai/cursor-sdk";

const cwd = process.argv[2] ?? process.cwd();
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
    model: { id: process.env.CURSOR_MODEL ?? "composer-2.5" }, // any id from Cursor.models.list()
    local: { cwd },
  });
  const run = await agent.send("List the files in this folder and read README.md, then tell me its first line.");
  for await (const event of run.stream()) {
    if (event.type === "tool_call") console.log(`tool ${event.name}: ${event.status}`);
  }
  const first = await run.wait();
  console.log("run 1:", first.status, first.usage);

  const second = await (await agent.send("Now say which file is the largest.")).wait();
  console.log("run 2:", second.status, second.usage, "\nagent (session id):", agent.agentId);
} finally {
  await agent?.[Symbol.asyncDispose]();
  // Export the spans, including a failed Agent.create (e.g. a bad CURSOR_API_KEY).
  await shutdown(tracerProvider);
}
