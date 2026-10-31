import type { Adapter } from "../../adapters.js";
import {
  CODEX_SCHEMA,
  CODEX_VERSIONS,
  MODELS,
  TOOLS,
  importCodex,
} from "./codec.js";
import { activation, preflightPlugin, registration } from "./plugin.js";
import { readImportSnapshot } from "./reader.js";
import { CodexObserver } from "./observer.js";
export function codexAdapter(hookEntry: string, version = "0.1.0"): Adapter {
  return {
    capability: {
      contract: 1,
      agent: "codex",
      surface: "cli",
      versions: CODEX_VERSIONS,
      schemas: [CODEX_SCHEMA],
      identityProfiles: ["codex-response-v1"],
      models: MODELS,
      providers: [],
      tools: TOOLS,
      platforms: ["darwin"],
      transcript: true,
      native: false,
      neutralResponse: "",
    },
    async preflight(action, context) {
      return preflightPlugin(action, context, hookEntry, version);
    },
    async probe(context) {
      return activation(context.socketPath);
    },
    normalize() {
      return [];
    }, // Anonymous hooks never mint LLM requests or usage.
    import: importCodex,
    readImport: readImportSnapshot,
    createObserver(context, services) {
      return new CodexObserver(context, services);
    },
    instructions(context) {
      const r = registration(context.socketPath);
      const quote = (s: string) => "'" + s.replace(/'/g, "'\\''") + "'";
      return [
        "codex plugin marketplace add " + quote(r.bundle),
        "codex plugin add traceai-codex@traceai-observers",
        "In Codex, use /hooks to review and approve the observer hooks; restart the session.",
        "fi-agent doctor codex --surface cli",
      ];
    },
  };
}
