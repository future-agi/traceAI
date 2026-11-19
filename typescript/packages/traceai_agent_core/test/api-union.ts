// Compile-only contract: each formerly forked adapter-facing field remains expressible.
import {
  type AdapterInspection,
  type CoreAdapter,
  type EndInput,
  type ResolveOptions,
  type ResolvedConfig,
  type SetupOptions,
  type TurnInput,
  type ChildInput,
  BoundedSpanProcessor,
  createTurnTracer,
  setupBaseline,
  setupDefaults,
  readSetupDefaults,
  selectSetupProfile,
  environmentCredentialsOnly,
  deliveryStatePath,
  uninstall,
  isSensitiveField,
  isSensitiveKey,
  redactValue,
} from "@traceai/agent-core";
import {
  withFileLock,
  writeProtected,
  writeProtectedUnderLock,
  updateJson,
  readProtected,
} from "@traceai/agent-core/files";
import { collectionEnabled } from "@traceai/agent-core/fence";
import { collectionEnabled as controlEnabled } from "@traceai/agent-core/control";
import { resolveConfig } from "@traceai/agent-core/hook-state";
import { createTurnTracer as tracerEntry } from "@traceai/agent-core/tracer";

export async function consumer(
  config: ResolvedConfig,
  options: SetupOptions,
  adapter: CoreAdapter,
) {
  const turnInput: TurnInput = {
    sessionId: "s",
    turnId: "t",
    unit: "run",
    timing: "reconstructed",
    usagePlan: "turn",
    gaps: ["UNKNOWN_SHAPE"],
    deferRoot: false,
    checkpoint: [],
    continuationOf: {
      traceId: "0".repeat(32),
      spanId: "1".repeat(16),
      traceFlags: 1,
    },
  };
  const child: ChildInput = {
    id: "c",
    kind: "generation",
    timing: "unknown",
    toolCallId: "call-1",
    contentSource: "reconstructed-rollout",
    sourceTiming: "message-interval",
    usageContribution: "exclusive",
    fidelity: "recorded-request",
    gap: "UNKNOWN_SHAPE",
  };
  const end: EndInput = {
    model: "model",
    provider: "provider",
    timeToFirstTokenMs: 1,
    gap: "UNKNOWN_SHAPE",
    usage: {
      input: 1,
      output: 1,
      cacheInInput: true,
      reasoningInOutput: true,
      costUsd: 0,
      costSource: "host-estimate",
      costEstimator: "catalog-v1",
    },
  };
  const tracer = createTurnTracer({
    config,
    protocol: "http/json",
    beforeExport: async () => true,
  });
  const turn = tracer.startTurn(turnInput);
  turn.event({ name: "notification", input: "safe" });
  turn.gap("UNKNOWN_SHAPE");
  turn.child(child).end(end);
  turn.end(end);
  await tracer.discard();
  const processor = new BoundedSpanProcessor(
    config,
    undefined,
    "http/protobuf",
  );
  processor.recordDropped(1, "SOURCE_LIMIT");
  await processor.drain(100);
  await processor.shutdown(100);
  await new BoundedSpanProcessor(
    config,
    undefined,
    async () => true,
    "http/json",
  ).shutdown();
  const file = config.paths.user;
  await writeProtected(file, "{}", undefined, false);
  await writeProtected(file, "{}", undefined, 1024 * 1024);
  await updateJson(file, (old) => old, { lockWaitMs: 200 });
  await updateJson(file, (old) => old, false);
  await updateJson(file, (old) => old, 1024 * 1024);
  await readProtected(file, false, 1024 * 1024);
  await withFileLock(file, async (check) => {
    await check();
    await writeProtectedUnderLock(file, "{}", undefined, 1024 * 1024);
  });
  await setupBaseline(options);
  await setupDefaults(options);
  await readSetupDefaults(options);
  await selectSetupProfile(options);
  await resolveConfig(options);
  await uninstall({ ...options, adapter });
  const inspection: AdapterInspection = {
    registered: true,
    pendingTurns: 1,
    recovery: "static",
    issues: [],
    finisher: { pending: [] },
    trust: "required",
  };
  const resolveOptions: ResolveOptions = options;
  await adapter.uninstallUnconfigured?.(resolveOptions);
  await adapter.runUnconfigured?.([], undefined);
  await adapter.update?.({ config, scope: "user" });
  return {
    inspection,
    collectionEnabled,
    controlEnabled,
    tracerEntry,
    environmentCredentialsOnly,
    deliveryStatePath,
    isSensitiveField,
    isSensitiveKey,
    redactValue,
  };
}
