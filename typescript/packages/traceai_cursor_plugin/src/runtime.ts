import { spawn } from "node:child_process";
import { isAbsolute, join } from "node:path";
import {
  collectionEnabled,
  createTurnTracer,
  resolveConfig,
  updateJson,
  type ResolvedConfig,
} from "@traceai/agent-core";
import { CursorObserver } from "./observer.js";
import {
  TranscriptReader,
  correlate,
  type TranscriptResult,
} from "./transcript.js";
import {
  claimPending,
  finishPending,
  handoffPending,
  pendingTurnIds,
  routeId,
  FINISHER_CAP_MS,
  type PendingTurn,
} from "./state.js";

const SETTLE_MS = 1500;
// Leave a bounded final second for local OTLP delivery; the process watchdog also bounds HTTP.
const EXPORT_RESERVE_MS = 1000;
const pause = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));
function source(claim: PendingTurn) {
  const events = claim.snapshot.events;
  const prompt = events.find((e) => e.event === "beforeSubmitPrompt")?.input;
  const output = [...events]
    .reverse()
    .find((e) => e.event === "afterAgentResponse")?.output;
  const transcript = events.find((e) => e.transcript)?.transcript;
  return { prompt, output, transcript };
}
function retryable(result: TranscriptResult): boolean {
  return (
    !result.gap ||
    [
      "TRANSCRIPT_UNAVAILABLE",
      "TRANSCRIPT_PARTIAL_TAIL",
      "TRANSCRIPT_READ_BUDGET",
    ].includes(result.gap)
  );
}
async function currentConfig(
  c: ResolvedConfig,
  claim: PendingTurn,
): Promise<ResolvedConfig> {
  return resolveConfig({
    agent: "cursor",
    env: { ...process.env, FI_AGENT_CONFIG_DIR: c.paths.configDir },
    projectDir: claim.snapshot.projectDir,
  });
}
async function exportTurn(
  c: ResolvedConfig,
  claim: PendingTurn,
  result: TranscriptResult,
): Promise<void> {
  // Claim first: acquiring the state mutex can wait while the user revokes privacy.
  // Destroy durable content before any replay, and never reuse a pre-claim policy snapshot.
  if (!(await finishPending(c, claim, "EXPORT_CLAIMED"))) return;
  let latest: ResolvedConfig | undefined;
  let revoked = false;
  const beforeExport = async (): Promise<boolean> => {
    if (revoked) return false;
    try {
      latest = await currentConfig(c, claim);
      revoked =
        !collectionEnabled(c) ||
        !collectionEnabled(latest) ||
        routeId(latest) !== claim.route ||
        routeId(c) !== claim.route;
    } catch {
      // Invalid/unreadable protected policy is a revocation, not an ambiguous export.
      revoked = true;
    }
    return !revoked;
  };
  if (!(await beforeExport())) {
    await finishPending(c, claim, "CANCELLED_CONFIG_OR_DISABLE", true);
    return;
  }
  const config = latest!;
  const observer = new CursorObserver(
    config,
    createTurnTracer({ config, beforeExport }),
    result,
  );
  try {
    const rank = (name: string) =>
      name === "beforeSubmitPrompt"
        ? 0
        : ["stop", "sessionEnd"].includes(name)
          ? 2
          : 1;
    claim.snapshot.events.sort(
      (a, b) => rank(a.event) - rank(b.event) || a.receivedAt - b.receivedAt,
    );
    for (const event of claim.snapshot.events) {
      if (!collectionEnabled(config)) {
        await observer.close(true);
        await finishPending(c, claim, "CANCELLED_DISABLED", true);
        return;
      }
      await observer.accept(event);
    }
    await observer.close();
    const s = source(claim);
    const full = !!correlate(result, s.prompt, s.output);
    await finishPending(
      c,
      claim,
      revoked
        ? "CANCELLED_CONFIG_OR_DISABLE"
        : observer.deliveryError
          ? "DELIVERY_" + observer.deliveryError
          : observer.gaps.has("EMPTY_SESSION")
            ? "EMPTY_SESSION"
            : full
              ? "EXPORTED_FULL"
              : "EXPORTED_FALLBACK",
      true,
    );
    await updateJson(join(c.paths.stateDir, "cursor-diagnostics.json"), () => ({
      schemaVersion: 1,
      gaps: [
        ...observer.gaps,
        ...config.issues,
        ...(claim.snapshot.gap ? [claim.snapshot.gap] : []),
      ],
      hostQualification: "CURSOR_3_SCHEMA",
      observedAt: new Date().toISOString(),
    }));
  } finally {
    await observer.close();
  }
}
async function detach(c: ResolvedConfig, claim: PendingTurn): Promise<void> {
  // No shell, stdin, receiver, or daemon: run the exact observer bundle next to this hook
  // through the already pinned Node interpreter. Only opaque claim metadata is in argv.
  const child = spawn(
    isAbsolute(process.argv0) ? process.argv0 : process.execPath,
    [
      __filename,
      "--finish",
      c.paths.configDir,
      claim.id,
      claim.token,
      ...(claim.snapshot.projectDir ? [claim.snapshot.projectDir] : []),
    ],
    { detached: true, stdio: "ignore", env: { ...process.env } },
  );
  await new Promise<void>((resolve, reject) => {
    child.once("error", reject);
    child.once("spawn", resolve);
  });
  child.unref();
  if (child.pid) await handoffPending(c, claim, child.pid);
}
/** Called after every hook, also recovering a crashed finisher without replaying any export claim. */
export async function processPending(c: ResolvedConfig): Promise<void> {
  const settleDeadline = Date.now() + SETTLE_MS;
  for (const id of await pendingTurnIds(c)) {
    const claim = await claimPending(c, id);
    if (!claim) continue;
    const s = source(claim);
    let result: TranscriptResult = { turns: [], gap: "TRANSCRIPT_UNAVAILABLE" };
    if (c.captureContent && s.prompt && s.transcript) {
      result = await new TranscriptReader(c).readSettled(
        s.transcript,
        s.prompt,
        s.output,
        {
          budgetMs: Math.max(
            0,
            Math.min(settleDeadline, claim.deadline - EXPORT_RESERVE_MS) -
              Date.now(),
          ),
          intervalMs: 100,
          enabled: () => collectionEnabled(c),
        },
      );
      if (
        collectionEnabled(c) &&
        !correlate(result, s.prompt, s.output) &&
        retryable(result) &&
        Date.now() < claim.deadline - EXPORT_RESERVE_MS
      ) {
        await detach(c, claim);
        continue;
      }
    }
    await exportTurn(c, claim, result);
  }
}
async function finisherMain(args: string[]): Promise<void> {
  const [mode, configDir, id, token, projectDir] = args;
  if (
    mode !== "--finish" ||
    args.length < 4 ||
    args.length > 5 ||
    !isAbsolute(configDir ?? "") ||
    !/^[a-f0-9]{64}$/.test(id ?? "") ||
    !/^[a-f0-9]{64}$/.test(token ?? "") ||
    (projectDir && !isAbsolute(projectDir))
  )
    return;
  let watchdog = setTimeout(() => process.exit(0), FINISHER_CAP_MS);
  try {
    const c = await resolveConfig({
      agent: "cursor",
      env: { ...process.env, FI_AGENT_CONFIG_DIR: configDir },
      projectDir,
    });
    const claim = await claimPending(c, id, token);
    if (!claim) return;
    clearTimeout(watchdog);
    watchdog = setTimeout(
      () => process.exit(0),
      Math.max(1, claim.deadline - Date.now()),
    );
    const s = source(claim),
      reader = new TranscriptReader(c);
    let result: TranscriptResult = { turns: [], gap: "TRANSCRIPT_UNAVAILABLE" };
    if (
      s.prompt &&
      s.transcript &&
      c.captureContent &&
      routeId(c) === claim.route
    ) {
      while (collectionEnabled(c)) {
        result = await reader.read(s.transcript);
        if (
          correlate(result, s.prompt, s.output) ||
          !retryable(result) ||
          Date.now() >= claim.deadline - EXPORT_RESERVE_MS
        )
          break;
        await pause(
          Math.min(
            250,
            Math.max(1, claim.deadline - EXPORT_RESERVE_MS - Date.now()),
          ),
        );
      }
    }
    await exportTurn(c, claim, result);
  } finally {
    clearTimeout(watchdog);
  }
}
if (require.main === module) {
  finisherMain(process.argv.slice(2))
    .catch(() => {})
    .finally(() => process.exit(0));
}
