import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import {
  ensurePrivateDirectory,
  readJson,
  writeProtected,
  updateJson,
} from "@traceai/agent-core/files";
import type { ResolvedConfig } from "@traceai/agent-core/config";
import type { Event } from "./events";
interface Binding {
  session: string;
  turn: string;
  parentAgent?: string;
  parentTurn?: string;
}
interface Parent {
  session: string;
  child: string;
  expires: number;
  turns: Record<string, Binding>;
  pending?: Binding & { call: string };
}
/** Native ancestry is persistent; each child execution needs its own dispatch binding. */
export async function correlate(
  event: Event,
  config: ResolvedConfig,
): Promise<Event | undefined> {
  const dir = join(config.paths.configDir, "codex-ancestry");
  const file = (id: string) =>
    join(
      dir,
      createHash("sha256")
        .update(
          JSON.stringify([
            config.generation,
            config.endpoint,
            config.projectName,
            id,
          ]),
        )
        .digest("hex") + ".json",
    );
  const alive = (p: Parent | undefined): p is Parent =>
    !!p && p.expires > Date.now();
  const bind = (p: Parent, source: string, target: Binding): Parent => ({
    ...p,
    // Bound retained coordinates, not a mutable 'current parent turn'. Delayed old
    // hooks must keep their old binding even after a new dispatch starts.
    turns: Object.fromEntries([
      ...Object.entries(p.turns ?? {})
        .filter(([t]) => t !== source)
        .slice(-63),
      [
        source,
        {
          session: target.session,
          turn: target.turn,
          parentAgent: target.parentAgent,
          parentTurn: target.parentTurn,
        },
      ],
    ]),
  });
  if (
    event.hook_event_name === "SubagentStart" &&
    event.agent_id &&
    event.turn_id
  ) {
    await ensurePrivateDirectory(dir);
    const active = await readJson<{ turn: string; expires: number }>(
      file(event.session_id) + ".active",
    );
    if (!active || active.expires < Date.now()) return { ...event, gap: true };
    const names = await fs.readdir(dir);
    if (names.length >= 1024) {
      for (const name of names.slice(0, 1024)) {
        if (!/^[a-f0-9]{64}\.json(?:\.active|\.dispatch)?$/.test(name))
          continue;
        const p = join(dir, name),
          value = await readJson<{ expires: number }>(p);
        if (value && value.expires < Date.now()) await fs.unlink(p);
      }
      if ((await fs.readdir(dir)).length >= 1024)
        return { ...event, gap: true };
    }
    const spawning = await readJson<Parent>(file(event.session_id));
    const ancestor = alive(spawning) ? spawning.turns[active.turn] : undefined;
    const target: Binding = ancestor
      ? {
          session: ancestor.session,
          turn: ancestor.turn,
          parentAgent: event.session_id,
          parentTurn: active.turn,
        }
      : { session: event.session_id, turn: active.turn };
    const child = event.agent_id,
      source = event.turn_id;
    await updateJson<Parent>(file(child), (old) =>
      bind(
        alive(old)
          ? old
          : {
              session: event.session_id,
              child,
              expires: Date.now() + 86400000,
              turns: {},
            },
        source,
        target,
      ),
    );
    return {
      ...event,
      session_id: target.session,
      turn_id: target.turn,
      parent_agent_id: target.parentAgent,
      parent_turn_id: target.parentTurn,
      source_session_id: child,
      source_turn_id: source,
      transcript_path: event.agent_transcript_path,
    };
  }

  // The synchronous pre-tool hook is an explicit dispatch boundary even when
  // the child starts before send_input's PostToolUse result is emitted.
  if (
    event.hook_event_name === "PreToolUse" &&
    event.dispatch_target &&
    event.tool_use_id &&
    event.turn_id
  ) {
    const child = await readJson<Parent>(file(event.dispatch_target));
    if (alive(child) && child.session === event.session_id) {
      // Native child hooks can carry the root session_id plus the executing
      // child in agent_id. Resolve that source identity, not the envelope session.
      const dispatcherId = event.agent_id ?? event.session_id;
      const dispatcher = await readJson<Parent>(file(dispatcherId));
      const ancestor = alive(dispatcher)
        ? dispatcher.turns[event.turn_id]
        : undefined;
      // Reuse is another execution, not another root. Resolve the dispatcher's
      // source turn exactly as for spawn, retaining its immediate parent lane.
      const pending = {
        session: ancestor?.session ?? event.session_id,
        turn: ancestor?.turn ?? event.turn_id,
        parentAgent: ancestor ? dispatcherId : undefined,
        parentTurn: ancestor ? event.turn_id : undefined,
        call: event.tool_use_id,
      };
      await updateJson<Parent>(file(child.child), (old) => ({
        ...old!,
        pending,
      }));
      await writeProtected(
        file(event.session_id + ":" + event.tool_use_id) + ".dispatch",
        JSON.stringify({
          child: child.child,
          ...pending,
          expires: Date.now() + 86400000,
        }),
      );
    }
  }
  if (event.hook_event_name === "PostToolUse" && event.tool_use_id) {
    const dispatchFile =
      file(event.session_id + ":" + event.tool_use_id) + ".dispatch";
    const dispatch = await readJson<
      Binding & { child: string; expires: number }
    >(dispatchFile);
    if (dispatch && dispatch.expires > Date.now()) {
      await updateJson<Parent>(file(dispatch.child), (old) => {
        if (!alive(old)) throw new Error("ANCESTRY_EXPIRED");
        const next = event.dispatch_turn
          ? bind(old, event.dispatch_turn, dispatch)
          : { ...old };
        if (next.pending?.call === event.tool_use_id) delete next.pending;
        return next;
      });
      await fs.unlink(dispatchFile).catch(() => {});
    }
  }

  let parent = await readJson<Parent>(file(event.agent_id ?? event.session_id));
  if (
    alive(parent) &&
    (parent.child === event.session_id ||
      (parent.session === event.session_id && parent.child === event.agent_id))
  ) {
    if (["SessionEnd", "SessionStart"].includes(event.hook_event_name)) return;
    if (
      event.hook_event_name === "UserPromptSubmit" &&
      event.turn_id &&
      !parent.turns?.[event.turn_id] &&
      parent.pending
    ) {
      const source = event.turn_id;
      await updateJson<Parent>(file(parent.child), (old) => {
        if (!alive(old)) throw new Error("ANCESTRY_EXPIRED");
        // PostToolUse can bind this exact source turn while this update retries.
        // That is the same dispatch, not a missing prompt or a new ancestry edge.
        if (old.turns?.[source]) {
          parent = old;
          return old;
        }
        if (!old.pending) throw new Error("DISPATCH_NOT_FOUND");
        const next = bind(old, source, old.pending);
        delete next.pending;
        parent = next;
        return next;
      });
    }
    const binding = event.turn_id ? parent.turns?.[event.turn_id] : undefined;
    // Never guess from lifetime ancestry or the parent's latest active turn.
    // Unknown child work remains separate/incomplete rather than entering a done journal.
    if (!binding) return { ...event, gap: true };
    if (event.hook_event_name === "UserPromptSubmit")
      await writeProtected(
        file(parent.child) + ".active",
        JSON.stringify({ turn: event.turn_id, expires: Date.now() + 86400000 }),
      );
    return {
      ...event,
      source_session_id: parent.child,
      source_turn_id: event.turn_id,
      session_id: binding.session,
      turn_id: binding.turn,
      agent_id: parent.child,
      parent_agent_id: binding.parentAgent,
      parent_turn_id: binding.parentTurn,
      hook_event_name: ["Stop", "Interrupt"].includes(event.hook_event_name)
        ? "SubagentStop"
        : event.hook_event_name,
      status:
        event.hook_event_name === "Interrupt" ? "cancelled" : event.status,
    };
  }
  if (event.hook_event_name === "UserPromptSubmit" && event.turn_id) {
    await ensurePrivateDirectory(dir);
    await writeProtected(
      file(event.session_id) + ".active",
      JSON.stringify({ turn: event.turn_id, expires: Date.now() + 86400000 }),
    );
  }
  if (["Stop", "Interrupt", "SessionEnd"].includes(event.hook_event_name)) {
    const active = await readJson<{ turn: string }>(
      file(event.session_id) + ".active",
    );
    if (!event.turn_id || active?.turn === event.turn_id)
      await fs.unlink(file(event.session_id) + ".active").catch(() => {});
  }
  return event;
}
