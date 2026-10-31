import type { Adapter } from "../../adapters.js";
import { AgentError } from "../../security/files.js";
import {
  HOOK_SCHEMA,
  STREAM_SCHEMA,
  TOOLS,
  VERSIONS,
  normalizeCursorHook,
  readCursorStream,
  type CursorSurface,
} from "./codec.js";
import {
  cursorChange,
  cursorProbe,
  type CursorInstallOptions,
} from "./install.js";

export function cursorAdapters(options: CursorInstallOptions): Adapter[] {
  return (["ide", "cli"] as const).map(
    (surface: CursorSurface): Adapter => ({
      capability: {
        contract: 1,
        agent: "cursor",
        surface,
        versions: VERSIONS[surface],
        schemas:
          surface === "cli" ? [HOOK_SCHEMA, STREAM_SCHEMA] : [HOOK_SCHEMA],
        identityProfiles: [
          "cursor-" + surface + "-v1",
          ...(surface === "cli" ? ["cursor-cli-stream-v1"] : []),
        ],
        models: [],
        providers: [],
        tools: TOOLS,
        platforms: ["darwin", "linux"],
        transcript: false,
        native: false,
        neutralResponse: "",
        observerSchema: HOOK_SCHEMA,
      },
      preflight: (action, context) =>
        cursorChange(action, surface, context, options),
      probe: (context) => cursorProbe(surface, context, options),
      normalize: (input, context) =>
        normalizeCursorHook(input, surface, context),
      import: (bytes, context) => {
        if (surface !== "cli")
          throw new AgentError("HELD_CURSOR_TRANSCRIPT_SCHEMA", 5);
        // Explicit imports are a disjoint source. Without a hook/stream alias, enabling both
        // would create duplicate turn/tool work. CLI enforces import-only mode for this route.
        return readCursorStream(bytes, context);
      },
    }),
  );
}
export {
  HOOK_SCHEMA,
  STREAM_SCHEMA,
  minimizeCursorEvent,
  normalizeCursorHook,
  readCursorStream,
} from "./codec.js";
