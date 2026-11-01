import { createConnection } from "node:net";
import { isAbsolute } from "node:path";
import { frame } from "@traceai/agent-core/client";
import {
  EVENTS,
  HOOK_SCHEMA,
  minimizeCursorEvent,
  type CursorSurface,
} from "./codec.js";

/** No config/credential/transcript reads, disk fallback, HTTP or permission response. */
export async function observeCursor(args: string[]): Promise<void> {
  const fields: Record<string, string> = {};
  if (args.length !== 8) return;
  for (let i = 0; i < args.length; i += 2) {
    if (
      !["--surface", "--event", "--route", "--socket"].includes(args[i]) ||
      fields[args[i]]
    )
      return;
    fields[args[i]] = args[i + 1];
  }
  const surface = fields["--surface"] as CursorSurface;
  if (
    !["ide", "cli"].includes(surface) ||
    fields["--route"] !== "cursor." + surface ||
    !EVENTS.includes(fields["--event"] as (typeof EVENTS)[number]) ||
    !isAbsolute(fields["--socket"] ?? "")
  )
    return;
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 1_048_576) return;
    chunks.push(Buffer.from(chunk));
  }
  const event = minimizeCursorEvent(
    JSON.parse(Buffer.concat(chunks).toString()),
    surface,
    fields["--event"],
  );
  const bytes = frame({
    protocol_version: 1,
    operation: "observe-source",
    route_ref: fields["--route"],
    source_schema: HOOK_SCHEMA,
    event,
  });
  await new Promise<void>((resolve) => {
    const socket = createConnection({ path: fields["--socket"] });
    const timer = setTimeout(finish, 20);
    function finish() {
      clearTimeout(timer);
      socket.destroy();
      resolve();
    }
    socket.once("connect", () => socket.write(bytes));
    socket.once("data", finish);
    socket.once("error", finish);
    socket.once("end", finish);
  });
}
