import type { CoreAdapter } from "@traceai/agent-core";
import { IDE_HOLD, IDE_VERSION, inspectIde, unregisterIde } from "./registration.js";
export { IDE_HOLD, IDE_VERSION } from "./registration.js";
export { IDE_QUALIFIED, QUALIFIED_IDE_VERSIONS } from "./qualification.js";

export const kiroIdeAdapter: CoreAdapter = {
  id: "kiro-ide", version: IDE_VERSION, requiresWorker: false,
  async detect() { return { supported: false, reason: IDE_HOLD }; },
  async install() { throw new Error(IDE_HOLD); },
  inspect: inspectIde,
  uninstall: unregisterIde,
};
