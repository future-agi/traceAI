import type { EndInput } from "@traceai/agent-core";

/** Opt-in contract for an extension's own subagent boundary. Never infer process ancestry.
 * Emit only while the named spawning tool is executing. IDs are local to the current prompt.
 * The observer verifies the parent exists and never accepts destinations or credentials here.
 */
export const SUBAGENT_CHANNEL = "futureagi:subagent:v1";
export type SubagentEvent =
  | {
      type: "start";
      id: string;
      parentToolCallId: string;
      name?: string;
      input?: string;
    }
  | {
      type: "request";
      id: string;
      parentId: string;
      model?: string;
      provider?: string;
      input?: string;
    }
  | { type: "end"; id: string; result?: EndInput };
export interface PiEventBus {
  emit(channel: string, data: unknown): void;
}
export function reportSubagent(bus: PiEventBus, event: SubagentEvent): void {
  try {
    bus.emit(SUBAGENT_CHANNEL, event);
  } catch {
    /* tracing never breaks a tool */
  }
}
