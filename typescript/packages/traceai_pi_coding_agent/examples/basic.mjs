// Trace one Pi Coding Agent SDK session in Future AGI.
//
//   npm install @traceai/pi-coding-agent @traceai/fi-core @earendil-works/pi-coding-agent
//   FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-pi-agent node basic.mjs
//
// Pi uses the model and credentials from ~/.pi/agent (or PI_CODING_AGENT_DIR).
import { ProjectType, register } from "@traceai/fi-core";
import { createAgentSession, SessionManager } from "@earendil-works/pi-coding-agent";
import { PiCodingAgentInstrumentation } from "@traceai/pi-coding-agent";

const tracerProvider = register({ projectType: ProjectType.OBSERVE });
const instrumentation = new PiCodingAgentInstrumentation({
  tracerProvider,
  // Content is hidden by default. Opt in to prompts, tool arguments and results:
  // traceConfig: { hideInputs: false, hideOutputs: false },
});

const { session } = await instrumentation.wrapCreateAgentSession(createAgentSession)({
  sessionManager: SessionManager.inMemory(),
});

try {
  await session.prompt("List the files in this folder and read README.md");
  console.log(session.getLastAssistantText() ?? "");
} finally {
  session.dispose();
  await tracerProvider.shutdown();
}
