// One real Pi SDK session traced by @traceai/pi-coding-agent and exported to
// Future AGI. Configuration comes from the environment:
//   FI_API_KEY, FI_SECRET_KEY, FI_BASE_URL, FI_PROJECT_NAME   (Future AGI)
//   PI_CODING_AGENT_DIR (agentDir with models.json), PI_E2E_CWD (project dir)
//   argv: <provider> <modelId> <prompt>
import { join } from "node:path";
import { ProjectType, register } from "@traceai/fi-core";
import { PiCodingAgentInstrumentation } from "@traceai/pi-coding-agent";
import { createAgentSession, ModelRuntime, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";

const [provider, modelId, prompt] = process.argv.slice(2);
const agentDir = process.env.PI_CODING_AGENT_DIR;
const cwd = process.env.PI_E2E_CWD;

const tracerProvider = register({ projectName: process.env.FI_PROJECT_NAME, projectType: ProjectType.OBSERVE });
const instrumentation = new PiCodingAgentInstrumentation({ tracerProvider });
const tracedCreateAgentSession = instrumentation.wrapCreateAgentSession(createAgentSession);

const modelRuntime = await ModelRuntime.create({ authPath: join(agentDir, "auth.json"), modelsPath: join(agentDir, "models.json") });
const model = modelRuntime.getModel(provider, modelId);
if (!model) throw new Error(`model ${provider}/${modelId} not in models.json`);

const { session } = await tracedCreateAgentSession({
  cwd,
  agentDir,
  model,
  modelRuntime,
  thinkingLevel: "off",
  sessionManager: SessionManager.inMemory(cwd),
  settingsManager: SettingsManager.inMemory({}),
  tools: ["read", "ls"],
});
try {
  await session.prompt(prompt);
  console.log(JSON.stringify({ sessionId: session.sessionId, answer: session.getLastAssistantText()?.slice(0, 200) }));
} finally {
  session.dispose();
  await tracerProvider.shutdown();
}
